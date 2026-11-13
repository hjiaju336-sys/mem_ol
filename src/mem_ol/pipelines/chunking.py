from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

EmbedFn = Callable[[Sequence[str]], Sequence[Sequence[float]]]

_SENTENCE_RE = re.compile(r"[^。！？!?；;\n]+[。！？!?；;]?")


@dataclass(frozen=True)
class SourceSegment:
    start: int
    end: int
    message_id: str | None = None
    turn_no: int | None = None
    role: str | None = None


@dataclass
class PlannedChunk:
    level: Literal["parent", "child"]
    chunk_no: str
    seq_no: int
    parent_index: int | None
    turn_no: int | None
    content: str
    char_start: int
    char_end: int
    turn_start: int | None
    turn_end: int | None
    message_ids: list[str] = field(default_factory=list)
    embedding: list[float] | None = None


@dataclass(frozen=True)
class _Unit:
    text: str
    start: int
    end: int


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def _trim_span(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


class SemanticChunker:
    """按段落/句子语义边界切父块，再在父块内切带重叠的子块。

    父块边界由相邻文本单元的向量相似度决定：低于阈值且达到最小长度时断开；
    同时受最大长度约束。子块按目标长度切分，并向后重叠若干字符。
    """

    def __init__(
        self,
        embed_fn: EmbedFn,
        *,
        parent_size: int = 600,
        parent_min: int = 250,
        parent_max: int = 900,
        child_size: int = 80,
        child_min: int = 30,
        child_max: int = 120,
        child_overlap: int = 20,
        semantic_threshold: float = 0.45,
        max_unit_size: int = 80,
    ) -> None:
        if not (0 < parent_min <= parent_size <= parent_max):
            raise ValueError("父块长度配置不合法")
        if not (0 < child_min <= child_size <= child_max):
            raise ValueError("子块长度配置不合法")
        if child_overlap < 0 or child_overlap >= child_size:
            raise ValueError("子块重叠必须小于子块目标长度")
        self.embed_fn = embed_fn
        self.parent_size = parent_size
        self.parent_min = parent_min
        self.parent_max = parent_max
        self.child_size = child_size
        self.child_min = child_min
        self.child_max = child_max
        self.child_overlap = child_overlap
        self.semantic_threshold = semantic_threshold
        self.max_unit_size = max_unit_size

    def split(
        self,
        text: str,
        segments: Sequence[SourceSegment] = (),
        *,
        batch_seq: int = 1,
        turn_no: int | None = None,
    ) -> list[PlannedChunk]:
        units = self._split_units(text)
        if not units:
            return []

        vectors = list(self.embed_fn([unit.text for unit in units]))
        if len(vectors) != len(units):
            raise ValueError("embed_fn 返回的向量数量与文本单元数量不一致")

        parent_ranges = self._group_parents(units, vectors)
        batch_prefix = f"B{batch_seq:04d}"
        if turn_no is not None:
            batch_prefix = f"{batch_prefix}-T{int(turn_no):04d}"

        planned: list[PlannedChunk] = []
        seq_no = 0
        for parent_index, (p_start, p_end) in enumerate(parent_ranges, start=1):
            start, end = _trim_span(
                text, units[p_start].start, units[p_end].end
            )
            parent_no = f"{batch_prefix}-P{parent_index:03d}"
            seq_no += 1
            turn_start, turn_end, message_ids = self._source_info(
                start, end, segments
            )
            planned.append(
                PlannedChunk(
                    level="parent",
                    chunk_no=parent_no,
                    seq_no=seq_no,
                    parent_index=parent_index,
                    turn_no=turn_no,
                    content=text[start:end],
                    char_start=start,
                    char_end=end,
                    turn_start=turn_start,
                    turn_end=turn_end,
                    message_ids=message_ids,
                )
            )

            for child_index, (c_start, c_end) in enumerate(
                self._split_children(units, p_start, p_end), start=1
            ):
                c_start, c_end = _trim_span(
                    text, units[c_start].start, units[c_end].end
                )
                seq_no += 1
                turn_start, turn_end, message_ids = self._source_info(
                    c_start, c_end, segments
                )
                planned.append(
                    PlannedChunk(
                        level="child",
                        chunk_no=f"{parent_no}-C{child_index:03d}",
                        seq_no=seq_no,
                        parent_index=parent_index,
                        turn_no=turn_no,
                        content=text[c_start:c_end],
                        char_start=c_start,
                        char_end=c_end,
                        turn_start=turn_start,
                        turn_end=turn_end,
                        message_ids=message_ids,
                    )
                )

        return planned

    # ---------- units ----------

    def _split_units(self, text: str) -> list[_Unit]:
        units: list[_Unit] = []
        offset = 0
        for line in text.splitlines(keepends=True):
            line_start = offset
            offset += len(line)
            raw = line.rstrip("\r\n")
            if not raw.strip():
                continue

            if len(raw) <= self.max_unit_size:
                units.append(_Unit(raw, line_start, line_start + len(raw)))
                continue

            buffer_start: int | None = None
            buffer_end: int | None = None
            buffer_len = 0
            for match in _SENTENCE_RE.finditer(raw):
                piece = match.group(0)
                if not piece.strip():
                    continue
                piece_start = line_start + match.start()
                piece_end = line_start + match.end()

                # 单句本身过长时硬切，保证每个文本单元都不超过 max_unit_size。
                if len(piece) > self.max_unit_size:
                    if buffer_start is not None and buffer_end is not None:
                        units.append(
                            _Unit(
                                text[buffer_start:buffer_end],
                                buffer_start,
                                buffer_end,
                            )
                        )
                        buffer_start = None
                        buffer_end = None
                        buffer_len = 0
                    for chunk_offset in range(
                        0, len(piece), self.max_unit_size
                    ):
                        sub_start = piece_start + chunk_offset
                        sub_end = min(
                            piece_start + chunk_offset + self.max_unit_size,
                            piece_end,
                        )
                        units.append(
                            _Unit(text[sub_start:sub_end], sub_start, sub_end)
                        )
                    continue

                if (
                    buffer_start is not None
                    and buffer_len + len(piece) > self.max_unit_size
                ):
                    units.append(
                        _Unit(
                            text[buffer_start:buffer_end],
                            buffer_start,
                            buffer_end,
                        )
                    )
                    buffer_start = None
                    buffer_len = 0
                if buffer_start is None:
                    buffer_start = piece_start
                buffer_end = piece_end
                buffer_len += len(piece)
            if buffer_start is not None and buffer_end is not None:
                units.append(
                    _Unit(text[buffer_start:buffer_end], buffer_start, buffer_end)
                )
        return units

    # ---------- parents ----------

    def _group_parents(
        self, units: Sequence[_Unit], vectors: Sequence[Sequence[float]]
    ) -> list[tuple[int, int]]:
        similarities = [
            _cosine(vectors[i - 1], vectors[i]) for i in range(1, len(units))
        ]

        ranges: list[tuple[int, int]] = []
        start_idx = 0
        current_len = len(units[0].text)

        for i in range(1, len(units)):
            unit_len = len(units[i].text)
            semantic_break = (
                current_len >= self.parent_min
                and similarities[i - 1] < self.semantic_threshold
            )
            size_break = (
                current_len >= self.parent_min
                and current_len + unit_len > self.parent_max
            )
            if semantic_break or size_break:
                ranges.append((start_idx, i - 1))
                start_idx = i
                current_len = unit_len
            else:
                current_len += unit_len

        ranges.append((start_idx, len(units) - 1))

        # 尾部过短的父块并入前一个，避免几十字的碎片父块。
        if len(ranges) > 1:
            last_start, last_end = ranges[-1]
            last_len = units[last_end].end - units[last_start].start
            if last_len < self.parent_min:
                prev_start, _ = ranges[-2]
                merged_len = units[last_end].end - units[prev_start].start
                if merged_len <= self.parent_max:
                    ranges[-2] = (prev_start, last_end)
                    ranges.pop()

        return ranges

    # ---------- children ----------

    def _split_children(
        self, units: Sequence[_Unit], p_start: int, p_end: int
    ) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        start = p_start

        while start <= p_end:
            end = start
            current_start = units[start].start
            current_end = units[end].end

            while end + 1 <= p_end:
                next_end = units[end + 1].end
                if next_end - current_start > self.child_max:
                    break
                if current_end - current_start >= self.child_size:
                    break
                end += 1
                current_end = units[end].end

            ranges.append((start, end))
            if end >= p_end:
                break

            next_start = end + 1
            overlap_floor = current_end - self.child_overlap
            while next_start > start + 1 and units[next_start - 1].start >= overlap_floor:
                next_start -= 1
            start = max(next_start, start + 1)

        # 尾部过短的子块并入前一个，避免产生几十字的碎片。
        if len(ranges) > 1:
            last_start, last_end = ranges[-1]
            last_len = units[last_end].end - units[last_start].start
            if last_len < self.child_min:
                prev_start, _ = ranges[-2]
                merged_len = units[last_end].end - units[prev_start].start
                if merged_len <= self.child_max:
                    ranges[-2] = (prev_start, last_end)
                    ranges.pop()

        return ranges

    # ---------- source mapping ----------

    @staticmethod
    def _source_info(
        start: int, end: int, segments: Sequence[SourceSegment]
    ) -> tuple[int | None, int | None, list[str]]:
        turns: list[int] = []
        message_ids: list[str] = []
        for segment in segments:
            if segment.start < end and segment.end > start:
                if segment.turn_no is not None:
                    turns.append(segment.turn_no)
                if segment.message_id and segment.message_id not in message_ids:
                    message_ids.append(segment.message_id)
        if not turns:
            return None, None, message_ids
        return min(turns), max(turns), message_ids
