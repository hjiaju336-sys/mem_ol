from __future__ import annotations

import os

import requests
import streamlit as st

API_BASE = os.getenv("MEM_OL_API_BASE", "http://127.0.0.1:8000/api")
TIMEOUT = 180

st.set_page_config(page_title="mem_ol · 游戏化叙事记忆", page_icon="🎲")


def request(method: str, path: str, **kwargs):
    try:
        response = requests.request(
            method, f"{API_BASE}{path}", timeout=TIMEOUT, **kwargs
        )
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001
        st.error(f"API 请求失败：{exc}")
        return None


def load_messages(session_id: str) -> list[dict]:
    return request("GET", f"/threads/{session_id}/messages") or []


def open_session(thread: dict) -> None:
    session_id = thread["id"]
    st.session_state.session_id = session_id
    st.session_state.session_name = thread["name"]
    messages = load_messages(session_id)
    if not messages:
        with st.spinner("生成开场白…"):
            request("POST", f"/threads/{session_id}/opening")
        messages = load_messages(session_id)
    st.session_state.messages = messages


def refresh_messages() -> None:
    st.session_state.messages = load_messages(
        st.session_state.session_id
    )


def rollback_to(turn_no: int, target_turn: int) -> bool:
    result = request(
        "POST",
        f"/threads/{st.session_state.session_id}/rollback",
        json={"target_turn": target_turn},
    )
    if result is None:
        return False
    st.session_state.flash = (
        f"已回退：保留到第 {target_turn} 轮，"
        f"删除 chunks={result.get('deleted_chunks', 0)}"
    )
    refresh_messages()
    return True


def retry_turn(turn_no: int, user_content: str) -> None:
    if not rollback_to(turn_no, turn_no - 1):
        return
    with st.spinner("重新生成本轮回复…"):
        data = request(
            "POST",
            f"/threads/{st.session_state.session_id}/messages",
            json={"content": user_content},
        )
    if data is None:
        st.session_state.flash = "回退成功，但重新生成失败。"
    else:
        st.session_state.flash = f"已重试第 {turn_no} 轮。"
    refresh_messages()
    st.rerun()


def group_turns(messages: list[dict]) -> dict[int, list[dict]]:
    turns: dict[int, list[dict]] = {}
    for message in messages:
        turn_no = int(message.get("turn_no") or 0)
        turns.setdefault(turn_no, []).append(message)
    return turns


def render_turn_actions(turn_no: int, user_content: str | None) -> None:
    left, right, _ = st.columns([1, 1, 6])
    with left:
        with st.popover("回退"):
            st.caption(f"删除第 {turn_no} 轮及之后的对话与记忆。")
            if st.button("确认回退", key=f"rollback-{turn_no}"):
                if rollback_to(turn_no, turn_no - 1):
                    st.rerun()
    with right:
        if user_content is None:
            return
        with st.popover("重试"):
            st.caption(
                f"删除第 {turn_no} 轮及之后的对话与记忆，"
                "然后用原来的玩家输入重新生成。"
            )
            if st.button("确认重试", key=f"retry-{turn_no}"):
                retry_turn(turn_no, user_content)


with st.sidebar:
    st.subheader("会话")
    new_name = st.text_input(
        "新会话名称",
        placeholder="例如：斗破·第一次冒险",
        key="new_session_name",
    )
    if st.button("＋ 新建会话", use_container_width=True):
        thread = request(
            "POST",
            "/threads",
            json={"name": new_name.strip() or "新会话"},
        )
        if thread:
            open_session(thread)
            st.rerun()

    st.divider()
    threads = request("GET", "/threads") or []
    if not threads:
        st.caption("还没有会话。")
    current_id = st.session_state.get("session_id")
    for thread in threads:
        selected = thread["id"] == current_id
        label = ("● " if selected else "") + thread["name"]
        if st.button(
            label,
            key=f"session-{thread['id']}",
            use_container_width=True,
            type="primary" if selected else "secondary",
        ):
            open_session(thread)
            st.rerun()

    st.divider()
    st.caption("先启动 API：`python main.py`")

st.title("🎲 游戏化叙事记忆 Demo")
if "session_id" not in st.session_state:
    st.info("在左侧选择或新建一个会话。")
    st.stop()

st.caption(f"当前会话：{st.session_state.get('session_name', '')}")

if flash := st.session_state.pop("flash", None):
    st.success(flash)

turns = group_turns(st.session_state.get("messages", []))
max_turn = max(turns) if turns else 0
retained_turns = set(range(max(1, max_turn - 4), max_turn + 1))

for turn_no in sorted(turns):
    user_content = None
    for message in turns[turn_no]:
        if message["role"] == "user":
            user_content = message["content"]
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
    if turn_no > 0 and turn_no in retained_turns:
        render_turn_actions(turn_no, user_content)

prompt = st.chat_input("你打算做什么？")
if prompt:
    with st.spinner("GM 正在回应…"):
        data = request(
            "POST",
            f"/threads/{st.session_state.session_id}/messages",
            json={"content": prompt},
        )
    if data is not None:
        st.session_state.flash = (
            "未入库轮次达到 5，已触发后台向量化。"
            if data.get("ingest_scheduled")
            else None
        )
        refresh_messages()
        st.rerun()
