"""Padrão v3 (issue #14): ficha por parte, roteiro de 15 s com trajeto, prompt de vários planos e nota de fidelidade.

Tudo aqui é verificável em dry_run (sem Flow, sem crédito). As faixas e metas vêm da configuração
(`script_words_target`, `script_words_tol`); nada de 25, 15 ou 60 espalhado como regra de negócio.
"""
import re

from content import VIDEO_SECONDS, band_from_settings, modern_script_problems, script_problems

# ---------- ficha visual (V3-1, V3-2) ----------
FICHA_SCHEMA = {"type": "object", "required": ["parts", "does_not_open"], "properties": {
    "parts": {"type": "array", "items": {"type": "object",
              "required": ["name", "color", "material", "movable", "visible_in_photo"],
              "properties": {"name": {"type": "string"}, "color": {"type": "string"}, "material": {"type": "string"},
                             "movable": {"type": "boolean"}, "opens_how": {"type": "string"},
                             "visible_in_photo": {"type": "boolean"}}}},
    "does_not_open": {"type": "array", "items": {"type": "string"}}}}


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _norm(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def ficha_issues(ficha):
    """V3-1: cada parte móvel diz como abre e o ficha diz o que NÃO abre. V3-2: cor e material por parte."""
    if not isinstance(ficha, dict):
        return ["ficha não é um objeto"]
    parts, closed = ficha.get("parts"), ficha.get("does_not_open")
    if not isinstance(parts, list) or not parts:
        return ["ficha sem partes"]
    problems = []
    if not isinstance(closed, list) or not all(_text(c) for c in closed):
        problems.append("ficha deve listar o que NÃO abre (does_not_open, mesmo que seja só o corpo principal)")
        closed = []
    names = set()
    for index, part in enumerate(parts, 1):
        if not isinstance(part, dict) or not _text(part.get("name")):
            problems.append(f"parte {index}: sem nome")
            continue
        name = part["name"].strip()
        names.add(_norm(name))
        for key, label in (("color", "cor"), ("material", "material")):
            if not _text(part.get(key)):
                problems.append(f"parte '{name}': falta {label}")
        if part.get("movable") is True and not _text(part.get("opens_how")):
            problems.append(f"parte móvel '{name}': diga como abre (direção do movimento)")
        if part.get("movable") is True and _norm(name) in {_norm(c) for c in closed}:
            problems.append(f"parte '{name}' está como móvel e também como 'não abre'")
    return problems


def allowed_actions(ficha):
    """Partes cuja ação o roteiro pode mostrar: móveis, com 'como abre', visíveis na foto e fora de 'não abre'."""
    closed = {_norm(c) for c in ficha.get("does_not_open", [])}
    return {_norm(p["name"]) for p in ficha.get("parts", [])
            if p.get("movable") is True and _text(p.get("opens_how")) and p.get("visible_in_photo") is True
            and _norm(p["name"]) not in closed}


# ---------- roteiro (V3-1, V3-3, C4) ----------
def script_v3_issues(script, ficha, name, banned=(), words=None, settings=None):
    """Portão do roteiro v3: regras do roteirista atual (faixa de palavras da config) + trajeto e ações da ficha."""
    words = words or band_from_settings(settings or {})
    problems = script_problems(script, words[1])
    problems += modern_script_problems(script, name, banned, hands_only=False, words=words)
    scene = script.get("use_scene")
    if not isinstance(scene, dict):
        return problems + ["falta use_scene (saída, trajeto e destino)"]
    for key, label in (("exit", "saída"), ("route", "trajeto"), ("destination", "destino")):
        if not _text(scene.get(key)):
            problems.append(f"cena de uso sem {label}")
    allowed = allowed_actions(ficha)
    for action in scene.get("actions") or []:
        part = _norm(action.get("part") if isinstance(action, dict) else "")
        if part not in allowed:
            problems.append(f"ação em '{part or '?'}' não está na ficha como parte móvel visível na foto; "
                            "ação que a foto não mostra fica fora do roteiro")
    return problems


# ---------- prompt do Flow (V3-4, V3-5, V3-6, V3-9) ----------
SHOTS = ("selfie com fala (gancho, câmera na mão, apresentador olhando para a câmera)",
         "detalhe da parte que vende",
         "uso em plano aberto",
         "POV do comando (mão no controle do produto)",
         "close final")
SPEED = ("câmera acompanhando de lado, fundo levemente borrado pela velocidade, roupa e cabelo ao vento")
TRAVEL_SECONDS, MAX_STILL_SECONDS = 5, 1.5


def video_prompt_v3(script, ficha, seconds=VIDEO_SECONDS):
    scene = script["use_scene"]
    parts = "; ".join(
        f"{p['name']}: {p['color']}, {p['material']}" + (f"; abre {p['opens_how']}" if p.get("movable") else "")
        for p in ficha["parts"])
    closed = ", ".join(ficha.get("does_not_open", [])) or "nada além do que foi dito"
    plans = " → ".join(f"{i}) {shot}" for i, shot in enumerate(SHOTS, 1))
    return "\n".join([
        f"Um único clipe vertical 9:16 de {seconds} segundos do produto da imagem de referência, com a voz nativa "
        "do próprio Flow (fala no clipe, sem narração por cima).",
        "Preserve exatamente formato, cores, proporções e componentes do produto. Não acrescente acessórios.",
        f"Partes, cores e materiais: {parts}.",
        f"O que NÃO abre nem se mexe: {closed}.",
        f"Vários planos dentro do mesmo clipe, nesta ordem: {plans}.",
        f"Cena de uso: sai de {scene['exit']}, passa por {scene['route']} e chega a {scene['destination']}.",
        f"Velocidade: {SPEED}.",
        f"Cerca de {TRAVEL_SECONDS} s de trajeto em movimento e no máximo ~{MAX_STILL_SECONDS} s parado no final.",
        "Placas, letreiros e fachadas aparecem sem texto escrito (formas e cores, nenhuma letra).",
        f"Fala em português do Brasil, voz natural e animada: \"{script['narration'].strip()}\" — termina até ~11 s "
        "(a edição fica em 10–12 s); o resto do clipe fica sem fala, só som ambiente.",
        "Sem textos, legendas, banners, logotipos ou marcas d'água na imagem. Sem preços, descontos ou promessas.",
    ])


# ---------- nota de fidelidade (V3-7) ----------
FIDELITY_NOTE_SCHEMA = {"type": "object", "required": ["colors", "moving_parts", "route", "destination", "score"],
                        "properties": {"colors": {"type": "array"}, "moving_parts": {"type": "array"},
                                       "route": {"type": "object"}, "destination": {"type": "object"},
                                       "score": {"type": "integer"}, "reason": {"type": "string"}}}


def fidelity_prompt(ficha, script):
    scene = script["use_scene"]
    return ("Compare o vídeo (quadros anexos) com a ficha e responda em JSON. Confira, um por um: "
            "(1) a cor de CADA parte da ficha; (2) a ação de CADA parte móvel (como abre, e que o que não abre "
            "continua fechado); (3) o trajeto; (4) o destino. "
            f"Ficha: {ficha['parts']}. Não abre: {ficha.get('does_not_open')}. "
            f"Trajeto esperado: de {scene['exit']} por {scene['route']}. Destino esperado: {scene['destination']}. "
            "Dê nota de 0 a 10 em \"score\" e escreva em \"reason\" o que divergiu.")


def fidelity_issues(note, ficha, script):
    """V3-7: a nota precisa cobrar cor por parte, ação das partes móveis, trajeto e destino."""
    if not isinstance(note, dict):
        return ["nota de fidelidade não é um objeto"]
    problems = []
    seen_colors = {_norm(c.get("part")) for c in note.get("colors") or [] if isinstance(c, dict)}
    for part in ficha["parts"]:
        if _norm(part["name"]) not in seen_colors:
            problems.append(f"nota sem a cor da parte '{part['name']}'")
    seen_moves = {_norm(m.get("part")) for m in note.get("moving_parts") or [] if isinstance(m, dict)}
    for part in ficha["parts"]:
        if part.get("movable") is True and _norm(part["name"]) not in seen_moves:
            problems.append(f"nota sem a ação da parte móvel '{part['name']}'")
    for key, label in (("route", "trajeto"), ("destination", "destino")):
        value = note.get(key)
        if not isinstance(value, dict) or not _text(value.get("seen")) or not isinstance(value.get("ok"), bool):
            problems.append(f"nota sem {label} (esperado: {{seen, ok}})")
    score = note.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 10:
        problems.append("score deve ser inteiro de 0 a 10")
    return problems


# ---------- orçamento (C2) ----------
def budget_allows(settings, used_today=0):
    """Único portão de orçamento do caminho v3, para qualquer origem (automático, do dia ou manual).

    Teto `video_daily_credits` = 0 recusa toda geração real; só o igaoeu muda o teto. Retorna (ok, motivo)."""
    cap, cost = int(settings["video_daily_credits"]), int(settings["video_clip_cost_expected"])
    if cap <= 0:
        return False, "teto diário de 0 créditos (video_daily_credits = 0): geração real recusada até o igaoeu definir o teto"
    if used_today + cost > cap:
        return False, f"teto diário de {cap} créditos atingido ({used_today} usados hoje)"
    return True, ""
