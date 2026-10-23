"""Personal document management; imports stay local until answer generation."""
import streamlit as st

from . import personal
from .progress import local_time
from .ui import page_heading, empty_state


def personal_page(settings, store):
    page_heading("个人信息", "管理简历和面试讲稿，让高分答案结合你的真实经历。", "个人资料")
    st.caption("文件在本机提取并保存，不上传 OSS。启用个性化后，生成答案会将检索到的片段、目标岗位及相关历史问答发送至 DeepSeek。请先移除不需要的联系方式等信息。")
    prefs = personal.profile(store)
    upload_tab, target_tab = st.tabs(["简历与面试讲稿", "目标岗位与生成偏好"])
    with target_tab:
        with st.form("personal-profile"):
            enabled = st.checkbox("生成高分答案时使用个人资料", value=prefs["enabled"])
            position = st.text_input("目标岗位", value=prefs["position"], max_chars=100)
            description = st.text_area("岗位描述（可选）", value=prefs["job_description"], max_chars=6000, height=160)
            length = st.selectbox("回答长度", personal.LENGTHS, index=personal.LENGTHS.index(prefs["length"]))
            if st.form_submit_button("保存生成偏好", type="primary"):
                try:
                    personal.save_profile(store, enabled=enabled, position=position, job_description=description, length=length)
                    st.success("已保存，下次生成生效。已有答案保持原样，可重新生成。")
                except ValueError as exc:
                    st.error(str(exc))
    with upload_tab:
        with st.form("personal-upload", clear_on_submit=True):
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
                        st.success("已保存并建立本地分段索引。请在「目标岗位与生成偏好」中启用个性化。")
                    except ValueError as exc:
                        st.error(str(exc))
                    except OSError:
                        st.error("文件保存失败，请检查磁盘空间及目录权限。")
        rows = personal.documents(store)
        st.caption(f"已保存 {len(rows)} / 50 份资料 · 删除文件不会改写已生成答案中的引用快照")
        if not rows:
            empty_state("先添加一份简历", "也可以上传项目面试讲稿，补充项目难点、职责和结果。")
        for row in rows:
            with st.container(border=True):
                st.subheader(row["filename"])
                st.caption(f"{row['kind']} · {'已启用' if row['active'] else '已停用'} · {row['characters']} 字符 · {local_time(row['created'])} · 版本 {row['id'][:8]}")
                with st.expander("预览提取文字"):
                    preview = st.checkbox("加载预览与原文件", key="personal-preview-enabled-" + row["id"])
                    document = store.one("SELECT text,original FROM personal_documents WHERE id=?", (row["id"],)) if preview else None
                    if document:
                        st.text_area("提取正文", document["text"], height=200, disabled=True, key="personal-preview-" + row["id"])
                        st.download_button("下载原文件", bytes(document["original"]), file_name=row["filename"], key="personal-download-" + row["id"])
                if st.button("停用此版本" if row["active"] else "启用此版本", key="personal-active-" + row["id"]):
                    personal.set_active(store, row["id"], not row["active"])
                    st.rerun()
                confirm = st.checkbox("确认永久删除此资料", key="personal-confirm-" + row["id"])
                if st.button("删除资料", disabled=not confirm, key="personal-delete-" + row["id"]):
                    personal.delete_document(store, row["id"])
                    st.rerun()
