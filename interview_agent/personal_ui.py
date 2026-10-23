"""Personal document management; imports stay local until answer generation."""
import streamlit as st

from . import personal
from .progress import local_time
from .ui import page_heading, empty_state


def personal_page(settings, store):
    page_heading("个人信息", "管理简历和面试讲稿，让高分答案结合你的真实经历。", "个人资料")
    prefs = personal.profile(store)
    rows = personal.documents(store)
    active = sum(bool(r["active"]) for r in rows)
    st.caption(f"{len(rows)} 份资料 · {active} 份启用 · 个性化{'已开启' if prefs['enabled'] else '未开启'}")
    upload_tab, target_tab = st.tabs(["我的资料", "生成偏好"])
    with target_tab:
        with st.form("personal-profile"):
            enabled = st.checkbox("生成高分答案时使用个人资料", value=prefs["enabled"])
            a, b = st.columns(2)
            position = a.text_input("目标岗位", value=prefs["position"], max_chars=100)
            length = b.selectbox("回答长度", personal.LENGTHS, index=personal.LENGTHS.index(prefs["length"]))
            with st.expander("补充岗位描述（可选）"):
                description = st.text_area("岗位描述（可选）", value=prefs["job_description"], max_chars=6000, height=130)
            if st.form_submit_button("保存生成偏好", type="primary"):
                try:
                    personal.save_profile(store, enabled=enabled, position=position, job_description=description, length=length)
                    st.success("已保存，下次生成生效。已有答案保持原样，可重新生成。")
                except ValueError as exc:
                    st.error(str(exc))
    with upload_tab:
        with st.expander("上传新资料", expanded=not rows), st.form("personal-upload", clear_on_submit=True, border=False):
            kind = st.selectbox("资料类型", personal.KINDS)
            upload = st.file_uploader("上传简历或面试讲稿", type=list(personal.FORMATS), max_upload_size=10, help="单份最多 10 MB、20 万字符。PDF 最多 100 页；扫描件需先 OCR，TXT/Markdown 使用 UTF-8。")
            st.caption("上传新版简历会停用旧版；面试讲稿和项目材料可同时启用多份。上传不调用模型。")
            if st.form_submit_button("保存个人资料", type="primary"):
                if upload is None:
                    st.warning("请先选择文件。")
                else:
                    try:
                        upload.seek(0)
                        personal.add_document(store, upload, upload.name, kind)
                        st.success("已保存并建立本地分段索引。请在「生成偏好」中启用个性化。")
                    except ValueError as exc:
                        st.error(str(exc))
                    except OSError:
                        st.error("文件保存失败，请检查磁盘空间及目录权限。")
        rows = personal.documents(store)
        if not rows:
            empty_state("还没有个人资料", "上传简历或面试讲稿，再到「生成偏好」开启个性化答案。")
        else:
            filter_column, document_column = st.columns([1, 2])
            category = filter_column.selectbox("按资料类型筛选", ["全部资料", *personal.KINDS], key="personal-kind-filter")
            filtered = [r for r in rows if category == "全部资料" or r["kind"] == category]
            if not filtered:
                st.info("此分类还没有资料，可在上方上传。")
            else:
                lookup = {r["id"]: r for r in filtered}
                selected = document_column.selectbox("选择资料", list(lookup), format_func=lambda key: f"{lookup[key]['filename']} · {lookup[key]['kind']} · {'启用' if lookup[key]['active'] else '停用'}", key="personal-selected-document")
                row = lookup[selected]
                with st.container(border=True, key="personal-detail"):
                    st.caption(f"{row['kind']} · {'已启用' if row['active'] else '已停用'} · {row['characters']} 字符")
                    document = store.one("SELECT text,original FROM personal_documents WHERE id=?", (selected,))
                    if document:
                        st.text_area("提取正文", document["text"], height=230, disabled=True, key="personal-preview-" + selected)
                        a, b = st.columns(2)
                        a.download_button("下载原文件", bytes(document["original"]), file_name=row["filename"], key="personal-download-" + selected, width="stretch")
                        if b.button("停用此版本" if row["active"] else "启用此版本", key="personal-active-" + selected, width="stretch"):
                            personal.set_active(store, selected, not row["active"])
                            st.rerun()
                    with st.expander("版本信息与删除"):
                        st.caption(f"上传于 {local_time(row['created'])} · 版本 {selected[:8]}")
                        st.caption("删除后旧答案中的引用快照及历史备份仍保留。")
                        confirm = st.checkbox("确认永久删除此资料", key="personal-confirm-" + selected)
                        if st.button("删除资料", disabled=not confirm, key="personal-delete-" + selected):
                            personal.delete_document(store, selected)
                            st.rerun()
        with st.expander("格式要求与资料使用说明"):
            st.caption("单份不超过 10 MB、20 万字符，最多保存 50 份。PDF 需为 100 页以内的文字版；扫描件先 OCR，TXT/Markdown 使用 UTF-8。")
            st.caption("上传及提取均在本机完成。开启个性化后，生成时会将相关资料片段、岗位描述和历史问答发送至 DeepSeek。请移除不需要的联系方式等信息。")
