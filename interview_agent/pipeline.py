from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError

from .config import Settings
from .db import BudgetExceeded, LostLease, Store, uid
from .diagnostics import describe_validation, repair_instructions, save_response
from .files import DataPathError, atomic_json, inside, read_json
from .media import AudioProcessingError, prepare, probe
from .providers import AliASR, DeepSeek, OSSStorage, OutputTruncated, ProviderError, normalize_asr
from .schemas import Extraction, Transcript, review_extraction
from .services import chunk_transcript, export_revision, purge_deleted


def job_settings(settings: Settings, snapshot: dict, checkpoint: dict | None = None) -> Settings:
    # Freeze output-affecting configuration, but use the current secrets/budget.
    current_fields = {"monthly_budget_cny", "audio_reserve_per_hour", "llm_reserve_per_chunk"}
    # Storage setup can be repaired until an object has been uploaded. Existing
    # remote objects must retain their original storage location on resume.
    has_remote_object = any(t.get("uploaded") or t.get("remote_task_id") for t in (checkpoint or {}).get("tracks", []))
    if not has_remote_object:
        current_fields |= {"oss_bucket", "oss_endpoint"}
    result = replace(settings, **{k: v for k, v in snapshot.items() if k in Settings.public_fields() and k not in current_fields})
    result.validate()
    return result


class Pipeline:
    def __init__(self, settings: Settings, store: Store, *, asr_factory=AliASR, llm_factory=DeepSeek, storage_factory=OSSStorage):
        self.settings, self.store = settings, store
        self.asr_factory, self.llm_factory, self.storage_factory = asr_factory, llm_factory, storage_factory
        self.owner = uid()

    def run_once(self) -> bool:
        job = self.store.claim(self.owner)
        if not job:
            return False
        stop = threading.Event()

        def renew():
            while not stop.wait(15):
                try:
                    if not self.store.heartbeat(job):
                        return
                    try:
                        atomic_json(self.settings.data_dir / "worker-status.json", {"updated": time.time()})
                    except OSError:
                        # The UI status file is advisory; it must not stop lease renewal.
                        pass
                except Exception:
                    # Main thread's next guarded operation will fail closed.
                    return

        heartbeat = threading.Thread(target=renew, daemon=True)
        heartbeat.start()
        try:
            self.store.event(job, "TASK_STARTED", "任务开始或从检查点恢复")
            self.process(job, job_settings(self.settings, job["config"], job["checkpoint"]))
            self.store.finish(job, "COMPLETED")
        except LostLease:
            pass
        except InterruptedError:
            self._finish(job, "CANCELLED", "任务已取消。")
        except BudgetExceeded as exc:
            self._finish(job, "FAILED", str(exc))
        except ProviderError as exc:
            self._finish(job, "FAILED", str(exc), retry=exc.retryable and not exc.unknown)
        except (DataPathError, AudioProcessingError) as exc:
            self._finish(job, "FAILED", str(exc))
        except PermissionError:
            self._finish(job, "FAILED", "本地文件暂时被占用或无写入权限，请关闭占用文件的程序后从检查点重试。")
        except FileNotFoundError:
            self._finish(job, "FAILED", "任务所需的本地文件不存在，请检查原录音和数据目录。")
        except (ValueError, OSError):
            # Do not leak pydantic inputs, subprocess output, or credential URLs.
            self._finish(job, "FAILED", "输入、数据校验或本地文件操作失败；请检查格式、磁盘权限和剩余空间。")
        except Exception:
            self._finish(job, "FAILED", "后台处理发生异常；检查配置后重试，已有检查点将保留。")
        finally:
            stop.set()
            heartbeat.join(timeout=2)
        return True

    def _finish(self, job, status, error, retry=False):
        try:
            self.store.finish(job, status, error, retry)
        except LostLease:
            pass

    def process(self, job: dict, settings: Settings):
        cp = job["checkpoint"]
        directory = inside(settings.data_dir, f"jobs/{job['interview_id']}/{job['id']}")
        directory.mkdir(parents=True, exist_ok=True)
        if not cp.get("transcript"):
            self.transcribe(job, settings, directory)
        self.store.check(job)
        transcript = Transcript.model_validate(read_json(inside(settings.data_dir, cp["transcript"])))
        self.store.checkpoint(job, "TRANSCRIPT_READY")
        if not cp.get("revision_id"):
            self.extract(job, settings, transcript, directory)
        self.store.check(job)
        self.store.checkpoint(job, "EXPORTING")
        export_revision(self.store, cp["revision_id"])

    def transcribe(self, job: dict, settings: Settings, directory: Path):
        cp = job["checkpoint"]
        missing = settings.missing_asr()
        if missing:
            raise ProviderError("录音转写配置缺失：" + "、".join(missing) + "。可先导入 TXT/JSON 使用整理。")
        if cp.get("submission_unknown"):
            raise ProviderError("SUBMISSION_UNKNOWN：上次提交状态不明，请先在阿里云确认，不自动重新扣费。")
        media = self.store.one("SELECT m.* FROM interviews i JOIN media m ON m.id=i.media_id WHERE i.id=?", (job["interview_id"],))
        if not media:
            raise ValueError("录音不存在")
        if "tracks" not in cp:
            self.store.checkpoint(job, "PREPROCESSING")
            source = inside(settings.data_dir, media["path"])
            info = probe(source)
            if info["duration_ms"] <= 0 or info["duration_ms"] > settings.max_audio_minutes * 60000:
                raise ProviderError("音频时长超过设置限制，第一版最多处理 120 分钟。")
            separate = info["channels"] == 2 and cp.get("stereo_mode") == "separate"
            tracks = []
            for n in range(2 if separate else 1):
                self.store.check(job)
                path = directory / f"audio-{n}.wav"
                prepare(source, path, n if separate else None)
                tracks.append({"path": str(path.relative_to(settings.data_dir)), "channel": n if separate else None, "object_key": f"interview-agent/{job['id']}/{n}.wav"})
            cp.update(tracks=tracks, duration_ms=info["duration_ms"])
            self.store.checkpoint(job)
        storage, asr = self.storage_factory(settings), self.asr_factory(settings)
        try:
            for n, track in enumerate(cp["tracks"]):
                if track.get("result_path"):
                    continue
                self.store.check(job)
                if not track.get("remote_task_id"):
                    if not track.get("uploaded"):
                        self.store.checkpoint(job, "ASR_UPLOADING")
                        # Keep a cleanup record for every upload attempt, even
                        # if an earlier interrupted upload used another bucket.
                        track["object_key"] = f"interview-agent/{job['id']}/{n}-{uid()}.wav"
                        self.store.checkpoint(job)
                        with self.store.transaction() as con:
                            self.store.guard(con, job)
                            con.execute("INSERT OR IGNORE INTO cleanup(id,job_id,object_key,config,eligible) VALUES (?,?,?,?,?)", (uid(), job["id"], track["object_key"], json.dumps({"oss_bucket": settings.oss_bucket, "oss_endpoint": settings.oss_endpoint}), time.time() + 49 * 3600))
                        storage.upload(track["object_key"], inside(settings.data_dir, track["path"]))
                        track["uploaded"] = True
                        job["config"]["oss_bucket"] = settings.oss_bucket
                        job["config"]["oss_endpoint"] = settings.oss_endpoint
                        with self.store.transaction() as con:
                            self.store.guard(con, job, allow_cancel=True)
                            con.execute("UPDATE jobs SET config=? WHERE id=?", (json.dumps(job["config"]), job["id"]))
                        self.store.checkpoint(job)
                    self.store.checkpoint(job, "ASR_SUBMITTING")
                    call_id = self.store.reserve(job, "aliyun", settings.asr_model, settings.audio_reserve_per_hour * cp["duration_ms"] / 3600000, settings.monthly_budget_cny)
                    cp["submission_unknown"] = True
                    track["call_id"] = call_id
                    self.store.checkpoint(job)
                    try:
                        task_id = asr.submit(storage.signed_url(track["object_key"]), diarization=track["channel"] is None)
                    except ProviderError as exc:
                        self.store.record_call(call_id, "unknown" if exc.unknown else "failed")
                        if not exc.unknown:
                            cp["submission_unknown"] = False
                            self.store.checkpoint(job)
                        raise
                    track["remote_task_id"] = task_id
                    track["submitted_at"] = time.time()
                    cp["submission_unknown"] = False
                    # Persist this result even when a cancellation arrived during submit.
                    self._persist_remote(job)
                    self.store.record_call(call_id, "submitted")
                self.store.checkpoint(job, "ASR_WAITING")
                while True:
                    self.store.check(job)
                    if time.time() - track["submitted_at"] > 20 * 3600:
                        raise ProviderError("远端任务等待超过 20 小时，请在控制台核对；不会自动重提。")
                    result = asr.poll(track["remote_task_id"])
                    status = result.get("output", {}).get("task_status")
                    cp["asr_status"] = {"PENDING": "排队中", "RUNNING": "识别中", "SUCCEEDED": "识别完成"}.get(status, status or "未知")
                    self.store.checkpoint(job)
                    if status == "SUCCEEDED":
                        raw = asr.result(result)
                        raw.pop("file_url", None)
                        normalized = normalize_asr(raw, track["channel"])
                        self.store.check(job)
                        result_path = directory / f"asr-{n}.json"
                        atomic_json(result_path, normalized.model_dump())
                        atomic_json(directory / f"asr-{n}-provider.json", raw)
                        track["result_path"] = str(result_path.relative_to(settings.data_dir))
                        self.store.checkpoint(job)
                        self.store.record_call(track["call_id"], "succeeded", result.get("usage", {}), result.get("request_id", ""))
                        with self.store.transaction() as con:
                            con.execute("UPDATE cleanup SET eligible=? WHERE object_key=?", (time.time(), track["object_key"]))
                        break
                    if status in ("FAILED", "CANCELED", "CANCELLED", "UNKNOWN"):
                        self.store.record_call(track["call_id"], "failed", result.get("usage", {}))
                        with self.store.transaction() as con:
                            con.execute("UPDATE cleanup SET eligible=? WHERE object_key=?", (time.time(), track["object_key"]))
                        raise ProviderError("远端转写任务失败或已取消，请在控制台核对。")
                    if status not in ("PENDING", "RUNNING"):
                        raise ProviderError("远端转写任务状态无法识别。")
                    for _ in range(10):
                        self.store.check(job)
                        time.sleep(1)
            segments = []
            for track in cp["tracks"]:
                segments.extend(Transcript.model_validate(read_json(inside(settings.data_dir, track["result_path"]))).segments)
            segments.sort(key=lambda s: (s.start_ms if s.start_ms is not None else 0, s.speaker_id or ""))
            for n, segment in enumerate(segments, 1):
                segment.id = f"s{n:05d}"
            # Provider duration is authoritative when it slightly exceeds container metadata.
            duration = max(cp["duration_ms"], max((s.end_ms or 0 for s in segments), default=0))
            transcript = Transcript(transcript_id=uid(), duration_ms=duration, segments=segments)
            path = f"transcripts/{transcript.transcript_id}/normalized.json"
            atomic_json(inside(settings.data_dir, path), transcript.model_dump())
            cp["transcript"] = path
            self.store.checkpoint(job)
        finally:
            asr.close()

    def _persist_remote(self, job):
        with self.store.transaction() as con:
            self.store.guard(con, job, allow_cancel=True)
            con.execute("UPDATE jobs SET checkpoint=?,updated=? WHERE id=?", (json.dumps(job["checkpoint"]), time.time(), job["id"]))

    def extract(self, job: dict, settings: Settings, transcript: Transcript, directory: Path):
        cp = job["checkpoint"]
        saved = cp.setdefault("chunks", [])
        if "chunk_plan" not in cp:
            # A previously completed prefix must retain the old grouping and
            # logical question IDs. Only unsaved chunks may be split further.
            initial = chunk_transcript(transcript, char_limit=9000, max_segments=None) if saved else chunk_transcript(transcript)
            cp["chunk_plan"] = [{"required": chunk["required"], "depth": 0} for chunk in initial]
        plan = cp["chunk_plan"]
        lookup = {segment.id: segment for segment in transcript.segments}
        if [sid for node in plan for sid in node["required"]] != [s.id for s in transcript.segments] or len(saved) > len(plan):
            raise ValueError("分块计划与转写版本不匹配")
        self.store.checkpoint(job, "EXTRACTING")
        llm = self.llm_factory(settings)
        try:
            existing, index = [], 0
            while index < len(plan):
                self.store.check(job)
                cp["current_chunk"] = index + 1
                node = plan[index]
                required = node["required"]
                overlap = plan[index - 1]["required"][-2:] if index else []
                chunk = {"segments": [lookup[sid] for sid in overlap + required], "required": required}
                if index < len(saved):
                    result = Extraction.model_validate(read_json(inside(settings.data_dir, saved[index])))
                else:
                    self.store.checkpoint(job)
                    self.store.event(job, "CHUNK_STARTED", "开始整理当前批次", chunk_no=index + 1, details={"total_chunks": len(plan), "segment_count": len(required), "first_segment": required[0], "last_segment": required[-1]})
                    repair = ""
                    did_split = False
                    for attempt in range(2):
                        count = self.store.one("SELECT COUNT(*) n FROM calls WHERE job_id=? AND provider='deepseek'", (job["id"],))["n"]
                        if count >= 64:
                            raise ProviderError("已达到本任务 64 次文本调用上限，停止自动尝试；请查看错误详情后处理。")
                        call_id = self.store.reserve(job, "deepseek", settings.llm_model, settings.llm_reserve_per_chunk, settings.monthly_budget_cny)
                        if hasattr(llm, "last_response"):
                            llm.last_response = None
                        cp["current_attempt"] = attempt + 1
                        cp["request_started_at"] = time.time()
                        self.store.checkpoint(job)
                        try:
                            result, usage, request_id = llm.extract(chunk["segments"], chunk["required"], existing[-8:], repair)
                            self.store.record_call(call_id, "received", usage, request_id)
                            result, warnings = review_extraction(result, chunk["segments"], set(required), {q["id"] for q in existing[-8:]}, {q["id"]: set(q["question_segment_ids"]) for q in existing[-8:]})
                            for warning in warnings:
                                cp.setdefault("validation_warnings", []).append(dict(warning, chunk_no=index + 1))
                                self.store.event(job, warning["code"], warning["message"], severity="warning", chunk_no=index + 1, attempt=attempt + 1, call_id=call_id, details=warning["details"])
                            self.store.event(job, "CHUNK_VALIDATED", "本批问答校验通过", chunk_no=index + 1, attempt=attempt + 1, call_id=call_id, details={"questions": len(result.items), "warnings": len(warnings)})
                        except OutputTruncated as exc:
                            self.store.record_call(call_id, "truncated", exc.usage, exc.request_id)
                            self.store.event(job, "OUTPUT_TRUNCATED", "输出达到长度上限", severity="warning", chunk_no=index + 1, attempt=attempt + 1, call_id=call_id)
                            if not self.split_pending(job, index, "length"):
                                raise ProviderError("当前片段多次拆分后仍达到输出上限，已保留完成的分块和原文，请检查该片段。") from None
                            did_split = True
                            break
                        except (ValidationError, ValueError) as exc:
                            self.store.record_call(call_id, "invalid_output", getattr(exc, "usage", None), getattr(exc, "request_id", ""))
                            issue = describe_validation(exc)
                            cp["last_diagnostic"] = dict(issue, chunk_no=index + 1, total_chunks=len(plan), attempt=attempt + 1, first_segment=required[0], last_segment=required[-1], resolved=False)
                            self.store.checkpoint(job)
                            self.store.event(job, issue["code"], issue["message"], severity="error", chunk_no=index + 1, attempt=attempt + 1, call_id=call_id, details=issue["details"])
                            repair = repair_instructions(issue)
                            if attempt:
                                if self.split_pending(job, index, "validation"):
                                    did_split = True
                                    break
                                raise ProviderError(f"第 {index + 1}/{len(plan)} 批（{required[0]}–{required[-1]}）未通过校验：[{issue['code']}] {issue['message']}。已保留原文与完成批次，请展开错误详情。") from None
                            continue
                        except ProviderError as exc:
                            self.store.record_call(call_id, "failed", exc.usage, exc.request_id)
                            self.store.event(job, "MODEL_REQUEST_FAILED", str(exc), severity="error", chunk_no=index + 1, attempt=attempt + 1, call_id=call_id, details={"finish_reason": exc.finish_reason, "retryable": exc.retryable})
                            raise
                        finally:
                            try:
                                artifact = save_response(directory, call_id, llm, index + 1, required)
                                if artifact:
                                    self.store.event(job, "RESPONSE_SAVED", "模型响应已保存在本机", chunk_no=index + 1, attempt=attempt + 1, call_id=call_id, details={"response_path": str(artifact.relative_to(settings.data_dir))})
                            except OSError:
                                self.store.event(job, "DIAGNOSTICS_WRITE_FAILED", "响应诊断文件写入失败；未影响已通过的问答校验。", severity="warning", chunk_no=index + 1, call_id=call_id)
                        break
                    if did_split:
                        continue
                    target = directory / f"chunk-{index}.json"
                    atomic_json(target, result.model_dump())
                    saved.append(str(target.relative_to(settings.data_dir)))
                    if cp.get("last_diagnostic"):
                        cp["last_diagnostic"]["resolved"] = True
                    self.store.checkpoint(job)
                for q in result.items:
                    qid = self.logical_id(job["id"], index, q.local_id)
                    if not q.continuation_of_id:
                        existing.append({"id": qid, "question": q.question, "question_segment_ids": q.question_segment_ids})
                index += 1
            self.store.checkpoint(job, "VALIDATING")
            content = self.merge(job, transcript)
            self.store.checkpoint(job, "PERSISTING")
            self.store.save_revision(job, content, cp["transcript"])
            cp["revision_id"] = content["revision_id"]
            self.store.checkpoint(job)
        finally:
            llm.close()

    def split_pending(self, job: dict, index: int, reason: str) -> bool:
        cp = job["checkpoint"]
        node = cp["chunk_plan"][index]
        required = node["required"]
        if len(required) < 2 or node["depth"] >= 4 or cp.get("split_count", 0) >= 12:
            return False
        middle = len(required) // 2
        cp["chunk_plan"][index:index + 1] = [{"required": part, "depth": node["depth"] + 1} for part in (required[:middle], required[middle:])]
        cp["split_count"] = cp.get("split_count", 0) + 1
        cp["split_reason"] = reason
        self.store.checkpoint(job)
        self.store.event(job, "CHUNK_SPLIT", "已拆分未完成批次，保留已完成批次", severity="warning", chunk_no=index + 1, details={"reason": reason, "depth": node["depth"] + 1, "total_chunks": len(cp["chunk_plan"])})
        return True

    @staticmethod
    def logical_id(job_id: str, chunk: int, local: str) -> str:
        return hashlib.sha256(f"{job_id}:{chunk}:{local}".encode()).hexdigest()[:32]

    def merge(self, job: dict, transcript: Transcript) -> dict:
        items, by_id, roles, corrections, dispositions = [], {}, {}, [], []
        for index, path in enumerate(job["checkpoint"]["chunks"]):
            result = Extraction.model_validate(read_json(inside(self.settings.data_dir, path)))
            for q in result.items:
                if q.continuation_of_id:
                    previous = by_id[q.continuation_of_id]
                    previous["question_segment_ids"] = list(dict.fromkeys(previous["question_segment_ids"] + q.question_segment_ids))
                    previous["answer_segment_ids"] = list(dict.fromkeys(previous["answer_segment_ids"] + q.answer_segment_ids))
                    if q.answer_summary:
                        previous["answer_summary"] = "\n".join(filter(None, [previous["answer_summary"], q.answer_summary]))
                        previous["answer_status"] = q.answer_status
                    previous["review_reasons"] = list(dict.fromkeys(previous["review_reasons"] + q.review_reasons))
                    continue
                entry = q.model_dump()
                entry["id"] = self.logical_id(job["id"], index, q.local_id)
                entry["parent_id"] = self.logical_id(job["id"], index, q.parent_local_id) if q.parent_local_id else q.parent_existing_id
                items.append(entry)
                by_id[entry["id"]] = entry
            for r in result.role_suggestions:
                old = roles.get(r.speaker_id)
                role = r.role if not old or old["role"] == r.role else "unknown"
                roles[r.speaker_id] = {"role": role, "display_name": r.speaker_id}
            corrections += [dict(c.model_dump(), status="pending") for c in result.correction_suggestions]
            dispositions += [d.model_dump() for d in result.segment_dispositions]
        all_refs = {sid for q in items for sid in q["question_segment_ids"] + q["answer_segment_ids"]}
        all_refs |= {d["segment_id"] for d in dispositions}
        if not {s.id for s in transcript.segments} <= all_refs:
            raise ValueError("合并后存在未处理片段")
        tags = list(dict.fromkeys(tag for q in items for tag in q["tags"]))
        pending = len({d["segment_id"] for d in dispositions if d["category"] == "needs_review"})
        summary = [f"本场共整理 {len(items)} 个问题。", "涉及主题：" + ("、".join(tags[:15]) or "未分类")]
        if pending:
            summary.append(f"另有 {pending} 个片段尚未归入问答，已保留原文待核对。")
        return {"schema_version": "1.0", "revision_id": self.logical_id(job["id"], -1, "revision"), "items": items, "speakers": roles, "corrections": corrections, "dispositions": dispositions, "validation_warnings": job["checkpoint"].get("validation_warnings", []), "review_status": "needs_review" if pending or job["checkpoint"].get("validation_warnings") or any(q["review_reasons"] for q in items) else "unreviewed", "summary": summary, "ai_advice": [], "model": job["config"].get("llm_model", "deepseek-flash"), "prompt_version": "1.2"}

    def maintenance(self):
        for row in self.store.rows("SELECT * FROM cleanup WHERE status='pending' AND eligible<=? AND attempts<12", (time.time(),)):
            try:
                settings = replace(self.settings, **json.loads(row["config"]))
                settings.validate()
                self.storage_factory(settings).delete(row["object_key"])
                with self.store.transaction() as con:
                    con.execute("UPDATE cleanup SET status='done',error='' WHERE id=?", (row["id"],))
            except Exception:
                with self.store.transaction() as con:
                    con.execute("UPDATE cleanup SET attempts=attempts+1,eligible=?,error='云端清理失败，请检查 OSS 配置' WHERE id=?", (time.time() + min(3600, 30 * 2 ** row["attempts"]), row["id"]))
        purge_deleted(self.store)
