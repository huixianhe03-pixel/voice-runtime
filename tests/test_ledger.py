"""账本：sample → 文本的映射。两条路都要测。"""

from __future__ import annotations

from conftest import run

from voice_runtime.audio import ms_to_samples, pcm_tone
from voice_runtime.events import E
from voice_runtime.ledger import ChunkRecord, GenerationHandle, GenerationLedger, TtsChunk, WordMark
from voice_runtime.providers.config import RuntimeConfig
from voice_runtime.scenarios import scenario_c


def _chunk(text: str, ms_per_char: float = 50.0) -> TtsChunk:
    n = ms_to_samples(len(text) * ms_per_char)
    import re

    parts = re.findall(r"\S+\s*", text)
    total = sum(len(p) for p in parts)
    acc = 0
    marks = []
    for i, p in enumerate(parts):
        end = n if i == len(parts) - 1 else acc + int(n * len(p) / total)
        marks.append(WordMark(p, acc, end))
        acc = end
    return TtsChunk(
        generation_id=1, chunk_index=0, text=text, text_span=(0, len(text)),
        pcm=pcm_tone(n), word_marks=tuple(marks),
    )


def test_word_marks_exclude_partially_played_word():
    """部分播出的尾词不算。宁可少认不可多认。"""
    chunk = _chunk("book the room now")
    rec = ChunkRecord(chunk=chunk, synthesized_at=0.0, enqueued=True)
    # 播到第三个词中间
    third = chunk.word_marks[2]
    rec.mark_played(third.start_sample + (third.end_sample - third.start_sample) // 2)
    heard = rec.heard_text()
    assert rec.mapping_method == "word_marks"
    assert heard == "book the "
    assert "room" not in heard, "播了一半的词被算成听到了"


def test_nothing_played_means_nothing_heard():
    rec = ChunkRecord(chunk=_chunk("book the room"), synthesized_at=0.0, enqueued=True)
    assert rec.heard_text() == ""
    assert rec.mapping_method == "not_played"


def test_not_enqueued_means_nothing_heard():
    """入队都没入，不可能听到。enqueued 只是意图，played 才是现实。"""
    rec = ChunkRecord(chunk=_chunk("book the room"), synthesized_at=0.0, enqueued=False)
    rec.mark_played(9999)
    assert rec.heard_text() == ""


def test_fully_played_returns_whole_text():
    chunk = _chunk("book the room")
    rec = ChunkRecord(chunk=chunk, synthesized_at=0.0, enqueued=True)
    rec.mark_played(chunk.n_samples)
    assert rec.heard_text() == "book the room"
    assert rec.mapping_method == "full_chunk"
    assert rec.fully_played


def test_played_samples_never_exceed_chunk():
    chunk = _chunk("book")
    rec = ChunkRecord(chunk=chunk, synthesized_at=0.0, enqueued=True)
    rec.mark_played(chunk.n_samples * 10)
    assert rec.played_samples == chunk.n_samples


def test_proportional_fallback_snaps_down_to_word_boundary():
    """降级路径：provider 不给 timing mark 时按比例换算再向下对齐。
    真实 provider 不一定给，所以这条路必须被测到。"""
    chunk = _chunk("book the room now")
    bare = TtsChunk(
        generation_id=1, chunk_index=0, text=chunk.text,
        text_span=chunk.text_span, pcm=chunk.pcm, word_marks=(),
    )
    rec = ChunkRecord(chunk=bare, synthesized_at=0.0, enqueued=True)
    rec.mark_played(int(bare.n_samples * 0.7))
    heard = rec.heard_text()
    assert rec.mapping_method == "proportional_snapped"
    assert heard in ("book the ", "book the room ")
    assert heard.endswith(" ") or heard == ""
    assert bare.text.startswith(heard)


def test_ledger_aggregates_the_four_quantities():
    led = GenerationLedger(GenerationHandle("s1", 1, 1))
    led.generated_text = "book the room now"
    c = _chunk("book the room now")
    rec = ChunkRecord(chunk=c, synthesized_at=0.0, enqueued=True)
    rec.mark_played(c.word_marks[1].end_sample)
    led.add(rec)
    led.was_interrupted = True

    assert led.heard_text() == "book the "
    assert led.enqueued_text() == "book the room now"
    assert led.heard_span() == (0, len("book the "))
    assert led.unheard_suffix() == "room now"
    assert led.context_content() == "book the "
    assert led.played_duration_ms() > 0


def test_context_uses_full_text_when_not_interrupted():
    led = GenerationLedger(GenerationHandle("s1", 1, 1))
    led.generated_text = "book the room now"
    led.was_interrupted = False
    assert led.context_content() == "book the room now"


def test_end_to_end_fallback_path_still_settles_correctly():
    """整条链路跑降级映射：被打断时依然只把听到的放进上下文。"""
    cfg = RuntimeConfig()
    cfg.tts.emit_word_marks = False
    r = run(scenario_c(cfg))
    p = [e for e in r.log.of(E.TURN_SETTLED) if e.generation_id == 1][
        0
    ].payload
    assert p["was_interrupted"] is True
    assert p["generated_text"].startswith(p["heard_text"])
    assert p["heard_text"] != p["generated_text"]
    methods = {rec.mapping_method for rec in r.session.ledgers[1].records}
    assert "proportional_snapped" in methods or "full_chunk" in methods
    assert r.metrics.stale_chunk_played_count == 0
