#!/usr/bin/env python3
"""Edição pós-geração (ffmpeg) do clipe de 15 s do Flow — issue #14, ED-1..ED-14.

O clipe bruto é a matéria-prima: a edição só corta e encurta (nunca pede outro clipe, ED-10, e este módulo nem
importa o Flow). Roda na hermes, sem rede e sem crédito, e é testável com um clipe de fixture (`make-fixture`).

CLI (ED-14): a saída é SEMPRE um JSON validado por RESULT_SCHEMA; o código de saída diz o resultado:
  0 ok · 2 argumentos/sem cortes · 3 transcrição falhou · 4 duração/fala fora da faixa · 5 plano parado · 6 ffmpeg · 7 banco
  · 8 resultado fora do schema (JSON de erro próprio) · 9 nota de fidelidade incompleta.
    video_edit.py deliver-raw --db DB --video-id N --product-key K --raw MP4 --dest DIR      (ED-13)
    video_edit.py edit --db DB --video-id N --out DIR [--words W.json] [--cuts C.json] [--dry-run] [--config J]
    video_edit.py frames --raw MP4 --out DIR                      (quadros a cada 0,5 s para o agente com visão)
    video_edit.py make-fixture --out DIR                          (clipe de teste de 15 s + transcrição por palavra)
"""
import argparse
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULTS = dict(edit_min_s=10.0, edit_max_s=12.0, hook_end_s=6.0, shot_target_s=2.0, shot_tol_s=0.5,
                static_max_s=1.5, word_margin_s=0.05, lead_silence_max_s=0.3, last_shot_min_s=0.5, last_shot_max_s=1.0,
                edit_tolerance_s=0.15)  # ED-1: folga da duração final em relação a edit_min_s/edit_max_s (parâmetro, não literal)
EXIT = {"ok": 0, "bad_args": 2, "transcription_failed": 3, "speech_too_long": 4, "too_short": 4, "bad_duration": 4,
        "static_shot": 5, "ffmpeg_failed": 6, "db_error": 7, "no_audio": 4, "raw_missing": 2, "cuts_missing": 2,
        "invalid_result": 8, "fidelity_failed": 9}
STATUSES = ("raw_delivered", "editing", "edited", "edit_failed")


class EditError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def config(overrides=None):
    cfg = dict(DEFAULTS)
    for key, value in (overrides or {}).items():
        if key in DEFAULTS:
            cfg[key] = float(value)
    return cfg


# ---------- SQLite (ED-12, ED-13, V3-8) ----------
def connect(path):
    db = sqlite3.connect(str(path))
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS clips (
        video_id INTEGER PRIMARY KEY, product_key TEXT, raw_path TEXT, edited_path TEXT, cuts TEXT,
        status TEXT NOT NULL, strategy TEXT, fidelity_note TEXT, fidelity_score INTEGER,
        credits INTEGER NOT NULL DEFAULT 0, dry_run INTEGER NOT NULL DEFAULT 0, error_code TEXT, error TEXT,
        created TEXT, updated TEXT)""")
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _upsert(db, video_id, **values):
    values["updated"] = _now()
    exists = db.execute("SELECT 1 FROM clips WHERE video_id=?", (video_id,)).fetchone()
    if exists:
        db.execute(f"UPDATE clips SET {', '.join(k + '=?' for k in values)} WHERE video_id=?",
                   (*values.values(), video_id))
    else:
        values.setdefault("status", "raw_delivered")
        values["created"] = values["updated"]
        db.execute(f"INSERT INTO clips(video_id,{','.join(values)}) VALUES(?,{','.join('?' * len(values))})",
                   (video_id, *values.values()))
    db.commit()


def get_clip(db, video_id):
    row = db.execute("SELECT * FROM clips WHERE video_id=?", (video_id,)).fetchone()
    return dict(row) if row else None


def deliver_raw(db, video_id, product_key, src, dest_dir):
    """ED-13: ponto definido de entrega do bruto — arquivo na pasta de destino + registro no SQLite, antes da edição."""
    src = Path(src)
    if not src.is_file():
        raise EditError("raw_missing", f"Clipe bruto não encontrado: {src}")
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / f"video_{video_id}_bruto.mp4"
    if src.resolve() != target.resolve():
        shutil.copy2(src, target)
    _upsert(db, video_id, product_key=product_key, raw_path=str(target), status="raw_delivered", credits=0)
    return str(target)


def save_strategy_and_note(db, video_id, strategy, note):
    """V3-8: estratégia escolhida e nota de fidelidade, por vídeo."""
    score = note.get("score") if isinstance(note, dict) else None
    _upsert(db, video_id, strategy=strategy, fidelity_note=json.dumps(note, ensure_ascii=False),
            fidelity_score=score if isinstance(score, int) and not isinstance(score, bool) else None)


# ---------- ffmpeg ----------
def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise EditError("ffmpeg_failed", f"{cmd[0]}: {error}") from None


def probe(path):
    done = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    if done.returncode:
        raise EditError("ffmpeg_failed", "ffprobe falhou: " + done.stderr[-200:])
    info = json.loads(done.stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise EditError("ffmpeg_failed", "arquivo sem vídeo")
    return dict(duration=float(info["format"]["duration"]), width=int(video["width"]), height=int(video["height"]),
                has_audio=any(s["codec_type"] == "audio" for s in info["streams"]))


def extract_frames(raw, out_dir, every=0.5):
    """Quadros a cada 0,5 s para o agente com visão escolher os cortes (ED-9)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    done = run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-vf", f"fps={1 / every},scale=-2:360",
                str(out / "q_%03d.jpg")])
    if done.returncode:
        raise EditError("ffmpeg_failed", done.stderr[-200:])
    return sorted(out.glob("q_*.jpg"))


def freeze_intervals(path, min_s):
    """Trechos congelados (freezedetect) com pelo menos `min_s` segundos, como (início, fim) — ED-4."""
    done = run(["ffmpeg", "-v", "info", "-i", str(path), "-vf", f"freezedetect=n=0.003:d={min_s}", "-an",
                "-f", "null", "-"])
    starts = [float(m) for m in re.findall(r"freeze_start: ([0-9.]+)", done.stderr)]
    ends = [float(m) for m in re.findall(r"freeze_end: ([0-9.]+)", done.stderr)]
    total = None
    out = []
    for index, start in enumerate(starts):
        if index < len(ends):
            end = ends[index]
        else:  # congelado até o fim: o ffmpeg não imprime o fim
            total = total or probe(path)["duration"]
            end = total
        out.append((start, end))
    return out


def static_segments(path, min_s):
    """Durações dos trechos congelados com pelo menos `min_s` segundos."""
    return [end - start for start, end in freeze_intervals(path, min_s)]


def dead_intervals(path, cfg, words):
    """ED-4: tempo morto = o que passa de static_max_s num plano congelado. Sai do meio do clipe (concat), sem
    cortar palavra: a borda que cairia dentro de uma fala recua/avança até a pausa mais próxima."""
    margin, keep = cfg["word_margin_s"], cfg["static_max_s"]
    dead = []
    for start, end in freeze_intervals(path, keep):
        a, b = start + max(0.0, keep - 0.3), end  # folga de 0,3 s: freezedetect e o concat mexem em 1–2 quadros
        for w in words:
            if w["start"] - margin < a < w["end"] + margin:
                a = w["end"] + margin
            if w["start"] - margin < b < w["end"] + margin:
                b = w["start"] - margin
        if b - a > 0.1:
            dead.append((round(a, 3), round(b, 3)))
    return dead


# ---------- transcrição por palavra (ED-8) ----------
def load_words(path):
    data = json.loads(Path(path).read_text())
    words = data["words"] if isinstance(data, dict) else data
    out = [dict(word=str(w["word"]), start=float(w["start"]), end=float(w["end"])) for w in words]
    if any(w["end"] < w["start"] for w in out):
        raise EditError("transcription_failed", "transcrição com palavra de fim antes do início")
    return sorted(out, key=lambda w: w["start"])


def whisper_words(raw, model="small"):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise EditError("transcription_failed", "faster-whisper indisponível; sem transcrição por palavra não há edição.") from None
    try:
        segments, _ = WhisperModel(model, device="cpu", compute_type="int8").transcribe(
            str(raw), language="pt", word_timestamps=True)
        return [dict(word=w.word.strip(), start=float(w.start), end=float(w.end)) for s in segments for w in s.words]
    except Exception as error:
        raise EditError("transcription_failed", f"transcrição falhou: {str(error)[:150]}") from None


# ---------- plano de cortes ----------
def inside_word(t, words, margin):
    return any(w["start"] - margin < t < w["end"] + margin for w in words)


def slide(t, words, margin, lo, hi):
    """ED-8: leva o corte para a pausa mais próxima dentro de [lo, hi]; None se não houver (o corte é descartado)."""
    if lo > hi:
        return None
    candidates = [t]
    for w in words:
        candidates += [w["start"] - margin, w["end"] + margin]
    ok = [c for c in candidates if lo - 1e-9 <= c <= hi + 1e-9 and not inside_word(c, words, margin - 1e-6)]
    return min(ok, key=lambda c: (abs(c - t), c)) if ok else None


def plan_cuts(duration, words, cfg, proposed=None, dead=()):
    """Define janela (início/fim no bruto) e cortes. Devolve dict(start, end, cuts, shots, keep, warnings).

    Os cortes de plano vêm SEMPRE de fora (`proposed`, ED-9: a rotina do agente olha os quadros): este módulo não
    inventa cortes de 2 s. `dead` = tempo morto a tirar do meio do clipe (ED-4)."""
    if not proposed:
        raise EditError("cuts_missing", "Sem --cuts: os cortes de plano vêm da rotina do agente com visão (ED-9); "
                                        "nenhum corte é inventado aqui.")
    if not words:
        raise EditError("transcription_failed", "Sem palavras na transcrição; a etapa não envia (ED-8).")
    margin, warnings = cfg["word_margin_s"], []
    first, last = words[0]["start"], words[-1]["end"]
    start = max(0.0, first - cfg["lead_silence_max_s"] / 2)       # ED-5: pausa antes da fala ≤ lead_silence_max_s
    if inside_word(start, words, margin - 1e-6):
        start = max(0.0, first - margin)
    need_end = last + margin
    if need_end - start > cfg["edit_max_s"] + 1e-6:
        raise EditError("speech_too_long", f"A fala vai de {first:.2f} s a {last:.2f} s e não cabe em "
                                           f"{cfg['edit_max_s']:.0f} s sem cortar palavra (ED-1/ED-7).")
    length = min(max(need_end - start + 0.4, cfg["edit_min_s"]), cfg["edit_max_s"])
    end = start + length
    removed = sum(max(0.0, min(b, end) - max(a, start)) for a, b in dead)   # tempo morto a retirar: a janela cresce para compensar
    end = min(start + length + removed, duration)
    if end > duration + 1e-6:
        end = duration
        if end - start < cfg["edit_min_s"] - 1e-6:
            raise EditError("too_short", f"O bruto (de {start:.2f} s a {duration:.2f} s) não rende {cfg['edit_min_s']:.0f} s.")
    targets = sorted(proposed)
    tol, cuts = cfg["shot_tol_s"], []
    for index, t in enumerate(targets):
        if not start + cfg["last_shot_min_s"] <= t <= end - cfg["last_shot_min_s"]:
            warnings.append(f"corte em {t:.2f} s fora da janela editada; descartado")
            continue
        lo, hi = t - tol, t + tol
        moved = slide(t, words, margin, max(lo, start + 0.1), min(hi, end - 0.1))
        if moved is None:
            warnings.append(f"corte em {t:.2f} s cairia dentro de uma palavra e não há pausa em ±{tol:g} s; descartado (ED-8)")
            continue
        if cuts and moved - cuts[-1] < cfg["last_shot_min_s"]:
            warnings.append(f"corte em {moved:.2f} s colado no anterior; descartado")
            continue
        cuts.append(round(moved, 3))
    bounds = [start] + cuts + [end]
    shots = [dict(start=round(a - start, 3), end=round(b - start, 3), raw_start=round(a, 3), raw_end=round(b, 3))
             for a, b in zip(bounds, bounds[1:])]
    for number, shot in enumerate(shots[1:], 2):  # ED-3: aviso quando o plano foge de shot_target_s ± shot_tol_s
        size = shot["end"] - shot["start"]
        is_last = number == len(shots)
        if not (abs(size - cfg["shot_target_s"]) <= tol + 1e-6 or
                (is_last and cfg["last_shot_min_s"] - 1e-6 <= size <= cfg["last_shot_max_s"] + 1e-6)):
            warnings.append(f"plano {number} com {size:.2f} s fora de {cfg['shot_target_s']:g}±{tol:g} s")
    keep, cursor = [], start
    for a, b in sorted(dead):
        a, b = max(a, start), min(b, end)
        if b - a > 1e-6:
            keep.append((cursor, a))
            cursor = b
    keep.append((cursor, end))
    keep = [(a, b) for a, b in keep if b - a > 1e-6]
    return dict(start=start, end=end, cuts=cuts, shots=shots, keep=keep, warnings=warnings)


def build_command(raw, out, start, end, keep=None):
    """ED-6/ED-7: só corte (e concat do tempo morto, ED-4) e recodificação; nenhum filtro de vídeo de texto/imagem
    (drawtext/subtitles/overlay) e áudio nativo mantido."""
    keep = keep or [(start, end)]
    tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(out)]
    if len(keep) == 1:
        a, b = keep[0]
        return ["ffmpeg", "-y", "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", str(raw),
                "-map", "0:v:0", "-map", "0:a:0"] + tail
    parts = "".join(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}];"
                    f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}];" for i, (a, b) in enumerate(keep))
    joined = "".join(f"[v{i}][a{i}]" for i in range(len(keep)))
    graph = f"{parts}{joined}concat=n={len(keep)}:v=1:a=1[v][a]"
    return ["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-filter_complex", graph, "-map", "[v]", "-map", "[a]"] + tail


# ---------- orquestração (ED-11) ----------
def edit_clip(db, video_id, out_dir, cfg=None, words=None, proposed=None, transcriber=whisper_words, dry_run=False):
    """Edita o bruto registrado. Em qualquer falha: nada é regerado, nenhum crédito, o bruto fica, o status é gravado."""
    cfg = cfg or config()
    clip = get_clip(db, video_id)
    if not clip or not Path(clip["raw_path"] or "").is_file():
        raise EditError("raw_missing", f"Vídeo {video_id} sem bruto entregue (ED-13).")
    raw, out = Path(clip["raw_path"]), Path(out_dir) / f"video_{video_id}_editado.mp4"
    _upsert(db, video_id, status="editing", dry_run=int(dry_run), credits=0, error_code=None, error=None)
    try:
        info = probe(raw)
        if not info["has_audio"]:
            raise EditError("no_audio", "O bruto não tem áudio; a fala nativa precisa estar no clipe (ED-7).")
        words = words if words is not None else transcriber(raw)
        dead = dead_intervals(raw, cfg, words) if proposed else []
        plan = plan_cuts(info["duration"], words, cfg, proposed, dead)
        out.parent.mkdir(parents=True, exist_ok=True)
        done = run(build_command(raw, out, plan["start"], plan["end"], plan["keep"]))
        if done.returncode or not out.is_file():
            raise EditError("ffmpeg_failed", "ffmpeg falhou: " + done.stderr[-200:])
        result = probe(out)
        tolerance = cfg["edit_tolerance_s"]
        if not (cfg["edit_min_s"] - tolerance <= result["duration"] <= cfg["edit_max_s"] + tolerance):
            raise EditError("bad_duration", f"Duração final {result['duration']:.2f} s fora de "
                                            f"{cfg['edit_min_s']:g}–{cfg['edit_max_s']:g} s (ED-1).")
        if not result["has_audio"]:
            raise EditError("no_audio", "O editado perdeu o áudio (ED-7).")
        long_static = [d for d in static_segments(out, cfg["static_max_s"]) if d > cfg["static_max_s"]]
        if long_static:
            raise EditError("static_shot", f"Plano parado por {max(long_static):.2f} s (máximo {cfg['static_max_s']:g} s, ED-4).")
    except EditError as error:
        out.unlink(missing_ok=True)  # nada parcial fica para ser enviado
        _upsert(db, video_id, status="edit_failed", edited_path=None, error_code=error.code, error=str(error), credits=0)
        raise
    _upsert(db, video_id, status="edited", edited_path=str(out), cuts=json.dumps(plan["shots"]), credits=0)
    return dict(video_id=video_id, status="edited", raw_path=str(raw), edited_path=str(out),
                duration=round(result["duration"], 3), cuts=plan["shots"], warnings=plan["warnings"],
                dead_removed=[list(d) for d in dead],
                lead_silence_s=round(words[0]["start"] - plan["start"], 3))


# ---------- fidelidade do EDITADO (V3-7) ----------
def fidelity_check(db, video_id, ficha, script, frames_dir=None):
    """Roda o vídeo editado pelo portão de fidelidade: quadros do editado para a rotina com visão (se `frames_dir`) e,
    se já há nota gravada, `v3.fidelity_issues` (cor por parte, ação das móveis, trajeto, destino). Sem nota: pendente."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from v3 import fidelity_issues, fidelity_prompt
    clip = get_clip(db, video_id)
    if not clip or clip["status"] != "edited" or not clip["edited_path"]:
        raise EditError("raw_missing", f"Vídeo {video_id} sem editado para conferir.")
    frames = [str(f) for f in extract_frames(clip["edited_path"], frames_dir)] if frames_dir else []
    note = json.loads(clip["fidelity_note"]) if clip["fidelity_note"] else None
    issues = fidelity_issues(note, ficha, script) if note is not None else ["sem nota de fidelidade do editado (pendente)"]
    return dict(ok=not issues, issues=issues, frames=frames, prompt=fidelity_prompt(ficha, script), score=clip["fidelity_score"])


# ---------- JSON da CLI (ED-14) ----------
RESULT_SCHEMA = {
    "type": "object", "required": ["ok", "video_id", "status", "raw_path", "edited_path", "duration", "cuts",
                                   "warnings", "credits", "dry_run", "error"],
    "properties": {
        "ok": {"type": "boolean"}, "video_id": {"type": "integer"},
        "status": {"type": "string", "enum": list(STATUSES) + ["failed"]},
        "raw_path": {"type": ["string", "null"]}, "edited_path": {"type": ["string", "null"]},
        "duration": {"type": ["number", "null"]}, "credits": {"type": "integer", "enum": [0]},
        "dry_run": {"type": "boolean"}, "warnings": {"type": "array", "items": {"type": "string"}},
        "cuts": {"type": "array", "items": {"type": "object", "required": ["start", "end", "raw_start", "raw_end"],
                                            "properties": {k: {"type": "number"} for k in ("start", "end", "raw_start", "raw_end")}}},
        "error": {"type": ["object", "null"], "required": ["code", "message"],
                  "properties": {"code": {"type": "string"}, "message": {"type": "string"}}}}}
_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None), "integer": int, "number": (int, float)}


def validate(instance, schema, path="$"):
    """Validador mínimo (type, enum, required, properties, items): sem dependência extra no worker."""
    errors = []
    kinds = schema.get("type")
    kinds = [kinds] if isinstance(kinds, str) else kinds
    if kinds and not any(isinstance(instance, _TYPES[k]) and not (isinstance(instance, bool) and k in ("integer", "number"))
                         for k in kinds):
        return [f"{path}: tipo {type(instance).__name__} não é {kinds}"]
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} fora de {schema['enum']}")
    if isinstance(instance, dict):
        errors += [f"{path}.{k}: obrigatório" for k in schema.get("required", []) if k not in instance]
        for key, sub in schema.get("properties", {}).items():
            if key in instance:
                errors += validate(instance[key], sub, f"{path}.{key}")
    if isinstance(instance, list) and "items" in schema:
        for i, item in enumerate(instance):
            errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


def emit(video_id, dry_run, ok_payload=None, error=None, clip=None):
    """ED-14: imprime o JSON da CLI. Se o resultado não passa no schema, imprime um JSON de erro próprio
    (code=invalid_result, também validado) e devolve o código 8, nunca o JSON inválido."""
    try:
        print(json.dumps(result_json(video_id, dry_run, ok_payload, error, clip), ensure_ascii=False))
        return EXIT.get(error.code, 1) if error else 0
    except RuntimeError as problem:
        fallback = EditError("invalid_result", str(problem)[:300])
        data = dict(ok=False, video_id=video_id, status="failed", raw_path=None, edited_path=None, duration=None, cuts=[],
                    warnings=[], credits=0, dry_run=bool(dry_run), error=dict(code=fallback.code, message=str(fallback)))
        print(json.dumps(data, ensure_ascii=False))
        return EXIT["invalid_result"]


def result_json(video_id, dry_run, ok_payload=None, error=None, clip=None):
    clip = clip or {}
    base = dict(ok=error is None, video_id=video_id, status="failed" if error else "edited",
                raw_path=clip.get("raw_path"), edited_path=None, duration=None, cuts=[], warnings=[], credits=0,
                dry_run=bool(dry_run), error=None)
    if error:
        base["status"] = clip.get("status") or "failed"
        base["error"] = dict(code=error.code, message=str(error))
    else:
        base.update({k: ok_payload[k] for k in ("status", "raw_path", "edited_path", "duration", "cuts", "warnings")})
    problems = validate(base, RESULT_SCHEMA)
    if problems:  # nunca imprime JSON fora do schema
        raise RuntimeError("saída fora do schema: " + "; ".join(problems))
    return base


# ---------- fixture ----------
def make_fixture(out_dir, seconds=15):
    """Clipe de teste: imagem em movimento (sem plano parado), tom de áudio e transcrição por palavra de ~25 palavras."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    clip, words = out / "fixture.mp4", out / "fixture.words.json"
    done = run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size=360x640:rate=25:duration={seconds}",
                "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(clip)])
    if done.returncode:
        raise EditError("ffmpeg_failed", done.stderr[-200:])
    data = [dict(word=f"p{i + 1}", start=round(0.9 + i * 0.44, 3), end=round(0.9 + i * 0.44 + 0.30, 3)) for i in range(25)]
    words.write_text(json.dumps(dict(words=data), ensure_ascii=False))
    return clip, words


# ---------- CLI ----------
def main(argv=None):
    parser = argparse.ArgumentParser(prog="video_edit")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("deliver-raw", "edit", "frames", "make-fixture", "fidelity"):
        p = sub.add_parser(name)
        if name in ("deliver-raw", "edit", "fidelity"):
            p.add_argument("--db", required=True)
            p.add_argument("--video-id", type=int, required=True)
        if name == "deliver-raw":
            p.add_argument("--product-key", required=True)
            p.add_argument("--raw", required=True)
            p.add_argument("--dest", required=True)
        if name == "edit":
            p.add_argument("--out", required=True)
            p.add_argument("--words")
            p.add_argument("--cuts")
            p.add_argument("--config")
            p.add_argument("--dry-run", action="store_true")
        if name == "fidelity":
            p.add_argument("--ficha", required=True)
            p.add_argument("--script", required=True)
            p.add_argument("--frames-out")
        if name == "frames":
            p.add_argument("--raw", required=True)
        if name in ("frames", "make-fixture"):
            p.add_argument("--out", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exit_:
        return 2 if exit_.code else 0
    video_id, dry_run = getattr(args, "video_id", 0), getattr(args, "dry_run", False)
    db = None
    try:
        if args.command == "make-fixture":
            clip, words = make_fixture(args.out)
            print(json.dumps(dict(ok=True, clip=str(clip), words=str(words))))
            return 0
        if args.command == "frames":
            print(json.dumps(dict(ok=True, frames=[str(f) for f in extract_frames(args.raw, args.out)])))
            return 0
        try:
            db = connect(args.db)
        except sqlite3.Error as error:
            raise EditError("db_error", str(error)) from None
        if args.command == "fidelity":
            result = fidelity_check(db, args.video_id, json.loads(Path(args.ficha).read_text()),
                                    json.loads(Path(args.script).read_text()), args.frames_out)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else EXIT["fidelity_failed"]
        if args.command == "deliver-raw":
            path = deliver_raw(db, args.video_id, args.product_key, args.raw, args.dest)
            clip = get_clip(db, args.video_id)
            payload = dict(status="raw_delivered", raw_path=path, edited_path=None, duration=None, cuts=[], warnings=[])
            return emit(video_id, False, payload, clip=clip)
        cfg = config(json.loads(args.config) if args.config else None)
        clip = get_clip(db, video_id) or {}
        words = load_words(args.words) if args.words else None
        if words is None:
            raw_path = clip.get("raw_path")
            sidecar = Path(raw_path).with_suffix(".words.json") if raw_path else None
            words = load_words(sidecar) if dry_run and sidecar and sidecar.is_file() else None
        proposed = json.loads(Path(args.cuts).read_text())["cuts"] if args.cuts else None
        payload = edit_clip(db, video_id, args.out, cfg, words, proposed, dry_run=dry_run)
        return emit(video_id, dry_run, payload, clip=get_clip(db, video_id))
    except EditError as error:
        clip = get_clip(db, video_id) if db and video_id else None
        return emit(video_id, dry_run, error=error, clip=clip)
    except (ValueError, KeyError, OSError) as error:  # arquivo de palavras/config inválido
        return emit(video_id, dry_run, error=EditError("bad_args", f"entrada inválida: {error}"))
    finally:
        if db:
            db.close()


if __name__ == "__main__":
    sys.exit(main())
