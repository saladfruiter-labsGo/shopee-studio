"""Passos do Flow descritos por texto/papel, lista de botões permitidos, custo e vídeo da geração atual.

Sem seletor fixo e sem mapa de rótulos aprendido: cada passo diz O QUE procurar (papel + padrões de texto ou
aria-label) e o worker resolve isso contra o que a tela mostra. Trocar um rótulo ("Enviar mídia" → "Fazer upload
de mídia") não quebra o passo. Tudo aqui é lógica pura, testável com uma tela de fixture (lista de elementos).
"""
import re
from urllib.parse import urlparse

FLOW_HOSTS = ("flow.google.com", "labs.google")

# Cada passo: papel do alvo e padrões (qualquer um casa, no texto OU no aria-label). Ordem = ordem do fluxo.
FLOW_STEPS = [
    dict(id="open_project", role="page", match=[r"flow"]),
    dict(id="agent_off", role="button", match=[r"^agente$"], expect_pressed=False),
    dict(id="open_settings", role="button", match=[r"gatilho de configura[cç][oõ]es", r"configura[cç][oõ]es"]),
    dict(id="pick_video", role="radio", match=[r"v[ií]deo$"]),
    dict(id="add_media", role="button", match=[r"adicionar elementos", r"adicionar m[ií]dia"]),
    dict(id="upload", role="button", match=[r"enviar m[íi]dia", r"fazer upload", r"upload de m[íi]dia"]),
    dict(id="include", role="button", match=[r"incluir no comando"]),
    dict(id="submit", role="button", match=[r"iniciar gera[cç][aã]o"]),
]
STEPS = {step["id"]: step for step in FLOW_STEPS}

# Única lista de rótulos que o worker pode clicar em botões de decisão/envio. "Sempre aprovar" nunca entra:
# desligaria a conferência de custo. A comparação é por igualdade (sem prefixo/substring).
ALLOWED_BUTTONS = {"aprovar", "rejeitar", "iniciar geração", "iniciar nova sessão",
                   "adicionar elementos à caixa de comando", "gatilho de configurações", "agente"}
FORBIDDEN_BUTTONS = re.compile(r"sempre\s+aprovar|always\s+approve", re.I)


class StepError(Exception):
    """`code` é o mesmo vocabulário do worker (ui_changed, cost_mismatch, ...)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _fold(value):
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def label_allowed(label):
    """True só para um botão da lista permitida (igualdade exata após normalizar caixa e espaços)."""
    text = _fold(label)
    return not FORBIDDEN_BUTTONS.search(text) and text in ALLOWED_BUTTONS


def assert_clickable(label):
    if not label_allowed(label):
        raise StepError("ui_changed", f"Clique recusado: '{label}' não está na lista de botões permitidos.")


def domain_ok(url):
    """Domínio travado no Flow: só https em flow.google.com / labs.google."""
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and parsed.hostname in FLOW_HOSTS


def resolve_step(step_id, elements):
    """Acha na tela (lista de {role, text, aria}) o alvo do passo, por papel e texto — nunca por seletor.

    Devolve o elemento ou levanta StepError('ui_changed'). Nunca devolve um botão proibido."""
    step = STEPS[step_id]
    patterns = [re.compile(p, re.I) for p in step["match"]]
    for element in elements:
        if step["role"] != "page" and element.get("role") != step["role"]:
            continue
        haystack = [_fold(element.get("text")), _fold(element.get("aria"))]
        if any(FORBIDDEN_BUTTONS.search(h) for h in haystack):
            continue
        if any(p.search(h) for p in patterns for h in haystack if h):
            return element
    raise StepError("ui_changed", f"Passo '{step_id}': nenhum elemento na tela casa com o papel "
                                  f"'{step['role']}' e os textos {step['match']}.")


def check_cost(read_cost, expected):
    """Custo lido ≠ custo esperado da configuração → cost_mismatch (nada é enviado). Sem valor fixo no código."""
    if expected is None or isinstance(expected, bool) or int(expected) <= 0:
        raise StepError("cost_mismatch", "Custo esperado ausente na configuração; nada foi enviado.")
    if read_cost is None or int(read_cost) != int(expected):
        raise StepError("cost_mismatch", f"O Flow mostra {read_cost} créditos; o esperado na configuração é "
                                         f"{expected}. Nada foi enviado.")
    return int(read_cost)


def budget_allows(settings, used_today=0):
    """Orçamento do dia: video_daily_credits = 0 recusa toda geração real (a edição e o dry_run custam 0)."""
    cap, cost = int(settings.get("video_daily_credits", 0)), int(settings.get("video_cost", 0))
    if cap <= 0:
        return False, "video_daily_credits = 0: geração real recusada até o igaoeu definir o teto."
    if used_today + cost > cap:
        return False, f"teto diário de {cap} créditos atingido."
    return True, ""


def is_current_generation(tile, submitted_at, image_name="", skew=30):
    """O vídeo baixado tem de ser o da geração atual (caso de 10/11: vídeo de outro produto).

    `tile` = {kind, thumb, created?, text}. Quando o Flow informa o instante de criação, ele precisa ser
    posterior ao envio (menos `skew` s); sem instante, vale só a âncora da galeria (scan_gallery)."""
    if tile.get("kind") != "video":
        return False
    created = tile.get("created")
    if created is not None and float(created) < float(submitted_at) - skew:
        return False
    return True
