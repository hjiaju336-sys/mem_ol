from __future__ import annotations

import json
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


def load_state(session_id: str) -> dict:
    return request("GET", f"/threads/{session_id}/state") or {}


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
    st.session_state.state_view = load_state(session_id)
    st.session_state.last_resolution = None
    st.session_state.last_effects = None


def refresh_messages() -> None:
    st.session_state.messages = load_messages(
        st.session_state.session_id
    )


def get_client_context(session_id: str) -> dict:
    """世界书/自定义提示词只存在前端会话，服务端不落库。"""
    if "client_contexts" not in st.session_state:
        st.session_state.client_contexts = {}
    return st.session_state.client_contexts.setdefault(
        session_id,
        {
            "custom_prompt": "",
            "lorebook": None,
            "pending_instructions": [],
        },
    )


def get_meta_notes(session_id: str) -> list[dict]:
    if "meta_notes" not in st.session_state:
        st.session_state.meta_notes = {}
    return st.session_state.meta_notes.setdefault(session_id, [])


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
    st.session_state.state_view = load_state(
        st.session_state.session_id
    )
    st.session_state.last_resolution = None
    st.session_state.last_effects = None
    return True


def retry_turn(turn_no: int, user_content: str) -> None:
    if not rollback_to(turn_no, turn_no - 1):
        return
    ctx = get_client_context(st.session_state.session_id)
    with st.spinner("重新生成本轮回复…"):
        data = request(
            "POST",
            f"/threads/{st.session_state.session_id}/messages",
            json={
                "content": user_content,
                "client_context": {
                    "custom_prompt": ctx.get("custom_prompt", ""),
                    "lorebook": ctx.get("lorebook"),
                },
            },
        )
    if data is None:
        st.session_state.flash = "回退成功，但重新生成失败。"
    else:
        st.session_state.flash = f"已重试第 {turn_no} 轮。"
        st.session_state.state_view = data.get("state_view", {})
        st.session_state.last_resolution = data.get("resolution")
        st.session_state.last_effects = data.get("applied_effects")
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
    if current_id:
        ctx = get_client_context(current_id)
        with st.expander("玩家自定义（仅前端）", expanded=False):
            custom_prompt = st.text_area(
                "自定义提示词",
                value=ctx.get("custom_prompt", ""),
                height=120,
                key=f"custom-prompt-{current_id}",
                help="长期生效。与核心规则冲突时以核心规则为准。",
            )
            ctx["custom_prompt"] = custom_prompt

            uploaded = st.file_uploader(
                "导入世界书（SillyTavern / Character Card V2）",
                type=["json"],
                key=f"lorebook-file-{current_id}",
            )
            if uploaded is not None:
                signature = f"{uploaded.name}:{len(uploaded.getvalue())}"
                signature_key = f"lorebook_signature_{current_id}"
                if st.session_state.get(signature_key) != signature:
                    try:
                        ctx["lorebook"] = json.loads(
                            uploaded.getvalue().decode("utf-8")
                        )
                        st.session_state[signature_key] = signature
                        st.success("世界书已载入当前前端会话。")
                    except Exception as exc:  # noqa: BLE001
                        st.error(f"世界书解析失败：{exc}")

            if ctx.get("lorebook"):
                raw = ctx["lorebook"]
                entries = raw.get("entries") if isinstance(raw, dict) else None
                count = len(entries) if isinstance(entries, (dict, list)) else 0
                st.caption(f"已载入世界书条目：{count}")
                st.download_button(
                    "导出世界书",
                    data=json.dumps(raw, ensure_ascii=False, indent=2),
                    file_name="lorebook.json",
                    mime="application/json",
                    key=f"lorebook-export-{current_id}",
                )
                if st.button("清除世界书", key=f"lorebook-clear-{current_id}"):
                    ctx["lorebook"] = None
                    st.rerun()

            pending = ctx.get("pending_instructions") or []
            if pending:
                st.caption("待生效的临时要求：")
                for item in pending:
                    st.markdown(f"- {item}")
                if st.button("清空临时要求", key=f"pending-clear-{current_id}"):
                    ctx["pending_instructions"] = []
                    st.rerun()

            st.caption("世界书和自定义提示词只保存在当前前端会话，服务端不落库。")

    st.divider()
    st.caption("先启动 API：`python main.py`")

st.title("🎲 游戏化叙事记忆内核 · 参考前端")
if "session_id" not in st.session_state:
    st.info("在左侧选择或新建一个会话。")
    st.stop()

st.caption(f"当前会话：{st.session_state.get('session_name', '')}")

if flash := st.session_state.pop("flash", None):
    st.success(flash)

meta_notes = get_meta_notes(st.session_state.session_id)
if meta_notes:
    latest = meta_notes[-1]
    st.info(f"系统回答（不进入剧情）：{latest['reply']}")
    if len(meta_notes) > 1:
        with st.expander("历史系统回答", expanded=False):
            for note in meta_notes[-10:]:
                st.markdown(f"**你：** {note['question']}")
                st.markdown(f"**系统：** {note['reply']}")

state_view = st.session_state.get("state_view") or {}
player_state = state_view.get("player") or {}
if player_state:
    left, middle, right, tags = st.columns([1, 1, 1, 2])
    left.metric("HP", f"{player_state.get('hp', 0)}/{player_state.get('max_hp', 0)}")
    middle.metric(
        "攻击",
        f"{player_state.get('attack', 0)}+{player_state.get('attack_bonus', 0)}",
    )
    right.metric("防御", player_state.get("defense", 0))
    tags.caption(
        "状态：" + ("、".join(player_state.get("tags") or []) or "正常")
    )
    time_label = (state_view.get("world") or {}).get("time_label")
    if time_label:
        st.caption(f"时间：{time_label}")

last_resolution = st.session_state.get("last_resolution")
last_effects = st.session_state.get("last_effects")
if last_resolution or last_effects:
    with st.expander("本轮结算", expanded=False):
        if last_resolution:
            st.write(f"结果：{last_resolution.get('outcome')}")
            for roll in last_resolution.get("rolls") or []:
                st.caption(
                    f"{roll.get('name')}：{roll.get('formula')} = "
                    f"{roll.get('total')} vs DC {roll.get('dc')}"
                )
            for fact in last_resolution.get("facts") or []:
                st.markdown(f"- {fact}")
            st.json(last_resolution.get("deltas") or [])
        for effect in last_effects or []:
            st.caption(
                f"剧情后果：{effect.get('path')} "
                f"{effect.get('before', '')}→{effect.get('after', '')}"
                f" {effect.get('value', '')}"
            )

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
    session_id = st.session_state.session_id
    ctx = get_client_context(session_id)
    with st.spinner("GM 正在回应…"):
        data = request(
            "POST",
            f"/threads/{session_id}/messages",
            json={
                "content": prompt,
                "client_context": {
                    "custom_prompt": ctx.get("custom_prompt", ""),
                    "pending_instructions": ctx.get("pending_instructions", []),
                    "lorebook": ctx.get("lorebook"),
                },
            },
        )
    if data is not None:
        if data.get("channel") == "meta":
            st.session_state.state_view = data.get("state_view", {})
            if data.get("meta_kind") == "question":
                get_meta_notes(session_id).append(
                    {
                        "question": prompt,
                        "reply": data.get("meta_reply") or "（无回答）",
                    }
                )
            else:
                pending = ctx.setdefault("pending_instructions", [])
                if prompt not in pending:
                    pending.append(prompt)
                if len(pending) > 3:
                    del pending[: len(pending) - 3]
            st.session_state.flash = None
            st.rerun()
        ctx["pending_instructions"] = []
        st.session_state.state_view = data.get("state_view", {})
        st.session_state.last_resolution = data.get("resolution")
        st.session_state.last_effects = data.get("applied_effects")
        st.session_state.flash = (
            "未入库轮次达到 5，已触发后台向量化。"
            if data.get("ingest_scheduled")
            else None
        )
        refresh_messages()
        st.rerun()
