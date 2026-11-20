"""Google Flow automation for the VM worker (Playwright, dedicated persistent Chromium profile).

Flow is driven through its project agent: the prompt goes in the prompt box with the product image
attached, the agent answers with a permission card ("... custa N créditos?"), and only an exact
"Aprovar" click spends credits. Every step before that click is free and safe to abort.
"""
import os
import re
import subprocess
import time
from pathlib import Path

from flow_steps import StepError, check_cost, domain_ok, find_option, resolve_step

CHROME = Path.home() / ".cache" / "ms-playwright" / "chromium-1234" / "chrome-linux64" / "chrome"
COST = re.compile(r"(\d+)\s*cr[eé]ditos?", re.I)
NO_CREDITS = re.compile(r"cr[eé]ditos? insuficientes|n[aã]o (tem|há) cr[eé]ditos|sem cr[eé]ditos|limite de cr[eé]ditos", re.I)
FAILED = re.compile(r"n[aã]o (foi|consegui) (poss[ií]vel )?gerar|falh(a|ou) na gera|viola|pol[ií]tica|n[aã]o posso gerar", re.I)
FAILED_CARD = re.compile(r"^\s*warning|falha|algo deu errado|tente de novo", re.I)
CHALLENGE = re.compile(r"n[aã]o sou um rob[oô]|unusual traffic|tr[aá]fego incomum|captcha|verify it'?s you|confirme que [eé] voc[eê]", re.I)
COST_PANEL = re.compile(r"usar\s+(\d+)\s*cr[eé]ditos", re.I)
# C1 (issue #14): nenhum seletor fixo (classe, tag de componente, aria-label). A tela é lida num SNAPSHOT de
# elementos (papel + texto + aria) e cada passo é resolvido por `flow_steps.resolve_step`: se o Flow renomear um
# rótulo ou trocar um componente, o passo continua achando o alvo pelo que ele DIZ, ou falha com `ui_changed`.
SNAPSHOT = r"""(root) => {
    const scope = root >= 0 ? document.querySelectorAll('[data-ss-tile]')[root] : document;
    if (!scope) return [];
    document.querySelectorAll('[data-ss]').forEach(e => e.removeAttribute('data-ss'));
    const roleOf = e => {
        const r = (e.getAttribute('role') || '').toLowerCase();
        if (r) return r;
        return e.tagName === 'BUTTON' ? 'button' : (e.isContentEditable ? 'textbox' : '');
    };
    const out = [];
    for (const e of scope.querySelectorAll('button, [role=button], [role=radio], [role=menuitem], [role=textbox], [contenteditable=true]')) {
        const role = roleOf(e);
        if (!role) continue;
        e.setAttribute('data-ss', String(out.length));
        out.push({i: out.length, role, text: (e.innerText || '').replace(/\s+/g, ' ').trim(),
                  aria: e.getAttribute('aria-label') || '', visible: e.offsetParent !== null || e.getClientRects().length > 0,
                  pressed: e.getAttribute('aria-pressed'), checked: e.getAttribute('aria-checked'),
                  disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true'});
    }
    return out;
}"""

# Galeria em ordem do DOM (mais recente primeiro). Um "tile" é o bloco mais externo cujo nome de elemento contém
# "tile"; o tipo vem do que ele contém (nome com error/pending/video/image), sem lista fixa de componentes:
# imagem enviada (com o nome do arquivo), vídeo pronto (miniatura), geração em andamento ou falha.
GALLERY = r"""() => {
    document.querySelectorAll('[data-ss-tile]').forEach(e => e.removeAttribute('data-ss-tile'));
    const isTile = e => /tile/i.test(e.tagName) && e.tagName.includes('-');
    const inTile = e => { for (let p = e.parentElement; p; p = p.parentElement) if (isTile(p)) return true; return false; };
    const tiles = [...document.querySelectorAll('*')].filter(e => isTile(e) && !inTile(e));
    return tiles.map((t, n) => {
        t.setAttribute('data-ss-tile', String(n));
        const text = (t.innerText || '').replace(/\s+/g, ' ').trim();
        const inner = [t, ...t.querySelectorAll('*')].filter(isTile).map(e => e.tagName.toLowerCase());
        const has = re => inner.some(name => re.test(name));
        if (has(/error/)) return {kind: 'error', text};
        if (has(/pending/)) return {kind: 'pending', text};
        if (has(/video/)) { const img = t.querySelector('img');
            return {kind: 'video', text, thumb: img ? img.src : ''}; }
        return {kind: has(/image/) ? 'image' : 'other', text};
    });
}"""

# Texto do painel de configurações: o menor ancestral que contém todas as opções (radios) visíveis do compositor,
# menos os cartões de aprovação. Sem lista de rótulos (360p, 15s...): vale o papel dos elementos.
SETTINGS_TEXT = r"""() => {
    const opts = [...document.querySelectorAll('[role=radio]')].filter(b => b.offsetParent !== null
        && !/^(aprovar|rejeitar)$/i.test(b.getAttribute('aria-label') || (b.innerText || '').trim()));
    if (!opts.length) return null;
    let node = opts[0].parentElement;
    while (node && !opts.every(o => node.contains(o))) node = node.parentElement;
    return node ? node.innerText : null;
}"""

# Cartões de confirmação em aberto: para cada opção "Aprovar" ainda livre, o texto do menor ancestral que fala de créditos.
CARDS = r"""() => [...document.querySelectorAll('[role=radio]')]
    .filter(r => /^aprovar$/i.test(r.getAttribute('aria-label') || r.innerText.trim())
        && r.getAttribute('aria-disabled') !== 'true' && r.getAttribute('aria-checked') !== 'true')
    .map(r => { let n = r.parentElement; while (n && !/cr.ditos?/i.test(n.innerText || '')) n = n.parentElement;
                return n ? n.innerText : ''; })"""

# Texto recente do chat do agente: elementos cujo nome contém "bubble".
BUBBLES = r"""() => [...document.querySelectorAll('*')].filter(e => e.tagName.includes('-') && /bubble/i.test(e.tagName))
    .map(e => e.innerText || '')"""

# Quantas imagens há no compositor: ancestral comum da caixa de texto e do botão de envio.
ATTACHED = r"""() => {
    const box = [...document.querySelectorAll('[contenteditable=true], [role=textbox]')].find(b => b.offsetParent !== null);
    if (!box) return 0;
    let node = box.parentElement;
    while (node && node.parentElement && !node.querySelector('button')) node = node.parentElement;
    for (let i = 0; i < 3 && node && node.parentElement; i++) node = node.parentElement;
    return node ? node.querySelectorAll('img').length : 0;
}"""


def scan_gallery(tiles, image_name, seen):
    """Separa o que está ACIMA da imagem enviada nesta tarefa — só ali pode estar o vídeo dela.

    A galeria é virtualizada: entre leituras e recarregamentos, vídeos antigos entram e saem do DOM.
    Comparar a lista inteira com a de antes do envio faz um vídeo antigo parecer "novo" (foi assim que
    vídeos de outros produtos foram entregues). A geração é sempre mais nova que o upload da imagem de
    referência, então a âncora é o tile `produto<id>.jpg`. Sem a âncora, o limite é o primeiro vídeo
    já conhecido; sem nenhum dos dois, não há como provar nada e a região fica vazia.
    """
    anchor = next((i for i, t in enumerate(tiles) if t.get("kind") == "image" and image_name
                   and image_name in (t.get("text") or "").split()), None)
    anchored = anchor is not None
    if anchor is None:
        anchor = next((i for i, t in enumerate(tiles) if t.get("kind") == "video" and t.get("thumb") in seen), 0)
    region = tiles[:anchor]
    return {"anchored": anchored,
            "fresh": [t["thumb"] for t in region if t.get("kind") == "video" and t.get("thumb")
                      and t["thumb"] not in seen],
            "pending": [t.get("text", "") for t in region if t.get("kind") == "pending"],
            "errors": [t.get("text", "") for t in region if t.get("kind") == "error"]}


class FlowError(Exception):
    """`code` tells the panel what to do; `charged` means credits may already be spent."""

    def __init__(self, code, message, charged=False):
        super().__init__(message)
        self.code, self.charged = code, charged


def ensure_display(display):
    number = display.lstrip(":")
    lock = Path(f"/tmp/.X{number}-lock")
    if lock.exists():
        try:
            os.kill(int(lock.read_text().strip()), 0)
            return None  # A live X server already owns this display.
        except (ValueError, ProcessLookupError, PermissionError):
            pass  # Stale lock from a crashed server: Xvfb below refuses to start if it is really in use.
    process = subprocess.Popen(["Xvfb", display, "-screen", "0", "1440x900x24", "-nolisten", "tcp"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        if Path(f"/tmp/.X11-unix/X{number}").exists() and process.poll() is None:
            return process
        time.sleep(0.1)
    raise FlowError("ui_changed", "Xvfb não iniciou.")


class Flow:
    def __init__(self, profile, display=":95", log=print):
        self.profile, self.display, self.log = str(profile), display, log
        self.playwright = self.context = self.page = None
        self.project_url = ""
        self.new_thumbs, self.seen_thumbs = [], set()

    def __enter__(self):
        from playwright.sync_api import sync_playwright  # Only the VM worker venv has Playwright.
        ensure_display(self.display)
        self.playwright = sync_playwright().start()
        try:
            # --no-sandbox only for this dedicated profile: approved by the user (Ubuntu blocks userns sandboxing).
            self.context = self.playwright.chromium.launch_persistent_context(
                self.profile, executable_path=str(CHROME) if CHROME.exists() else None, headless=False,
                args=["--no-sandbox", "--window-size=1440,900", "--no-first-run", "--no-default-browser-check"],
                viewport={"width": 1440, "height": 900}, accept_downloads=True, locale="pt-BR",
                env={**os.environ, "DISPLAY": self.display})
        except Exception as error:
            self.playwright.stop()
            busy = "ProcessSingleton" in str(error) or "SingletonLock" in str(error) or "in use" in str(error)
            raise FlowError("browser_busy" if busy else "ui_changed",
                            "Perfil do Flow em uso por outro navegador (login em andamento?)." if busy
                            else f"Chromium não abriu: {str(error)[:200]}") from None
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(30000)
        return self

    def __exit__(self, *exc):
        try:
            if self.context:
                self.context.close()
        finally:
            if self.playwright:
                self.playwright.stop()

    # ---------- helpers ----------
    def screenshot(self, path):
        try:
            self.page.screenshot(path=str(path))
        except Exception:
            pass

    def text(self):
        return self.page.evaluate("document.body ? document.body.innerText : ''")

    # ---------- leitura da tela (C1) ----------
    def screen(self, tile=-1):
        """Snapshot dos controles da tela (papel, texto, aria, estado). `tile` limita a um tile da galeria."""
        return self.page.evaluate(SNAPSHOT, tile)

    def locate(self, element):
        return self.page.locator(f'[data-ss="{element["i"]}"]').first

    def step(self, step_id, timeout=10, tile=-1):
        """Resolve o passo contra a tela atual (re-lendo até `timeout`) e devolve o elemento. Nunca clica."""
        deadline, error = time.time() + timeout, None
        while True:
            try:
                return resolve_step(step_id, self.screen(tile))
            except StepError as caught:
                error = caught
            if time.time() >= deadline:
                raise FlowError(error.code, str(error)) from None
            time.sleep(0.5)

    def click_step(self, step_id, timeout=10, tile=-1):
        """Clica no alvo que o passo resolveu. Os botões de decisão passam pela lista fechada (`gated`)."""
        element = self.step(step_id, timeout, tile)
        try:
            self.locate(element).click(timeout=5000)
        except Exception:  # Controles que só aparecem no hover: clique pelo DOM.
            self.page.evaluate("(i) => { const e = document.querySelector(`[data-ss='${i}']`); if (e) e.click(); }", element["i"])
        return element

    def check_session(self):
        url = self.page.url
        if "accounts.google.com" in url or "/signin" in url:
            raise FlowError("login_required", "Sessão do Google expirada: faça login no navegador da VM.")
        if CHALLENGE.search(self.text()):
            raise FlowError("challenge", "O Google pediu verificação humana (CAPTCHA ou confirmação de identidade).")

    # ---------- steps ----------
    def open_project(self, url):
        if not domain_ok(url):
            raise FlowError("no_project", "Configure a URL do projeto do Flow nas Preferências.")
        self.project_url = url
        self.page.goto(url, wait_until="domcontentloaded", timeout=90000)
        deadline = time.time() + 90
        while time.time() < deadline:
            self.check_session()
            if self.has_prompt_box():
                return
            if self.page.locator('a[href*="accounts.google.com/ServiceLogin"], a[href*="signin"]').count():
                raise FlowError("login_required", "Flow aberto sem login: faça login no navegador da VM.")
            time.sleep(1)
        raise FlowError("ui_changed", "Caixa de comando do Flow não apareceu.")

    def has_prompt_box(self):
        return any(e["role"] == "textbox" and e["visible"] for e in self.screen())

    def settings_text(self):
        return self.page.evaluate(SETTINGS_TEXT)

    def open_settings(self):
        """Abre o painel do compositor (botão de configurações, achado pelo texto), se ainda não estiver aberto."""
        if self.settings_text() is None:
            self.click_step("open_settings")
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.settings_text() is not None:
                return
            time.sleep(0.5)
        raise FlowError("ui_changed", "Painel de configurações da caixa de comando não abriu.")

    def pick(self, label):
        """Marca uma opção do painel (radio achado pelo texto). Clica de verdade: o clique via JS é ignorado."""
        try:
            option = find_option(label, self.screen())
        except StepError as error:
            raise FlowError(error.code, str(error)) from None
        if option["checked"] == "true":
            return
        self.locate(option).click()
        time.sleep(0.5)
        if find_option(label, self.screen())["checked"] != "true":
            raise FlowError("ui_changed", f"O Flow não marcou a opção '{label}'.")

    def agent_off(self):
        """Garante o modo Agente DESLIGADO: só assim o compositor mostra a chave Imagem/Vídeo.

        Com o Agente ligado o Flow não oferece a escolha de gerar vídeo e o resultado sai errado,
        então o worker confere o estado do botão em toda tarefa e desliga se estiver ligado.
        """
        for _ in range(4):
            button = self.step("agent_off", timeout=5)
            if button["pressed"] != "true":
                return
            self.click_step("agent_off")
            time.sleep(3)
        raise FlowError("ui_changed", "Não consegui desligar o modo Agente do Flow; sem isso não há chave de vídeo.")

    def ensure_settings(self, model, expected_cost=None, duration="15s"):
        """Agente desligado + compositor em Vídeo, Elementos, 9:16, 720p, 15 s (padrão v3), x1 e modelo esperado.

        Devolve o custo que o Flow mostra para a geração. Com o Agente desligado não existe cartão
        de confirmação: os créditos saem no envio, então quem chama avisa o painel antes.
        """
        self.agent_off()  # Sem isso o compositor não oferece a chave Imagem/Vídeo.
        self.open_settings()
        try:
            resolve_step("pick_video", self.screen())
        except StepError:
            raise FlowError("ui_changed", "O painel do compositor não oferece a opção Vídeo "
                                          "(modo Agente ainda ativo?).") from None
        # Elementos: as imagens anexadas são referências do produto (em Frames, 2 imagens viram quadro inicial/final).
        for label in ("Vídeo", "Elementos", "9:16", "720p", duration, "x1"):
            self.pick(label)
        picker = self.step("model_picker", timeout=5)
        current = re.sub(r"arrow_drop_down", "", picker["text"] or picker["aria"]).strip()
        if model.casefold() not in current.casefold():
            raise FlowError("model_mismatch", f"Modelo de vídeo no compositor é '{current}', esperado '{model}'.")
        cost = COST_PANEL.search(self.settings_text() or "")
        if not cost:
            raise FlowError("ui_changed", "O Flow não mostrou o custo da geração no compositor.")
        if expected_cost is not None:
            try:
                check_cost(int(cost.group(1)), expected_cost)  # custo esperado vem da configuração, sem valor fixo
            except StepError as error:
                raise FlowError(error.code, str(error)) from None
        self.page.keyboard.press("Escape")  # O painel salva conforme muda; fechar libera a caixa de comando.
        time.sleep(1)
        return int(cost.group(1))

    def new_session(self):
        """Abre uma sessão nova para o agente não herdar cartões/vídeos da tarefa anterior."""
        try:
            self.click_step("new_session", timeout=5)
            time.sleep(2)
        except FlowError:
            self.log("Flow: botão de nova sessão ausente; seguindo na sessão atual.")
        for _ in range(4):
            if self.has_prompt_box():
                return
            self.page.keyboard.press("Escape")  # Um painel aberto deixa a caixa oculta.
            time.sleep(2)
        raise FlowError("ui_changed", "Caixa de comando do Flow não ficou visível.")

    def attached(self):
        return self.page.evaluate(ATTACHED)

    def overlay_button(self, pattern, timeout=15):
        """Primeiro botão visível da tela cujo texto ou aria-label casa com `pattern` (regex), lido do snapshot."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            for element in self.screen():
                if element["role"] in ("button", "menuitem") and element["visible"] and \
                        pattern.search((element["text"] or "") + " " + (element["aria"] or "")):
                    return element
            time.sleep(0.5)
        return None

    def attach_image(self, image, expected=1):
        """Envia a imagem e a insere na caixa de comando; `expected` = quantas imagens a caixa deve ter depois."""
        name = Path(image).name
        self.click_step("add_media")
        try:
            upload = self.step("upload", timeout=15)
        except FlowError:
            raise FlowError("ui_changed", "Botão de upload do seletor de mídia não encontrado "
                                          "(o Flow mudou o rótulo).") from None
        with self.page.expect_file_chooser(timeout=15000) as chooser:
            self.locate(upload).click()
        chooser.value.set_files(str(image))
        item = self.overlay_button(re.compile(re.escape(name)), timeout=90)
        if item is None:
            raise FlowError("ui_changed", f"A imagem {name} não apareceu no seletor de mídia depois do upload.")
        for _ in range(60):  # The item exists while still uploading; wait until it is clickable.
            found = self.overlay_button(re.compile(re.escape(name)), timeout=1)
            if found and not found["disabled"]:
                item = found
                break
            time.sleep(1)
        self.locate(item).click()
        # Algumas versões inserem a imagem no clique do item; outras mostram a prévia e pedem "Incluir no comando".
        for _ in range(3):  # O botão é re-renderizado enquanto a imagem sobe e pode sair do DOM no clique.
            try:
                self.step("include", timeout=3)
            except FlowError:
                break
            try:
                self.click_step("include", timeout=3)
                break
            except Exception:
                self.page.wait_for_timeout(1000)
        for _ in range(30):
            if self.attached() >= expected:
                return
            time.sleep(1)
        raise FlowError("ui_changed", f"A imagem {name} não entrou na caixa de comando.")

    def attach_images(self, images):
        """Anexa as referências uma a uma e confere que a caixa tem exatamente essa quantidade (nada sobrando de
        uma tarefa anterior, nada faltando). Tudo antes do envio: falhar aqui não gasta crédito."""
        for number, image in enumerate(images, 1):
            self.attach_image(image, expected=number)
        count = self.attached()
        if count != len(images):
            raise FlowError("ui_changed", f"A caixa de comando ficou com {count} imagem(ns); o esperado era {len(images)}.")

    def submit(self, prompt):
        """Envia o comando e confirma pelo estado do compositor.

        O Flow novo mantém o clique em "Iniciar geração" e não aceita Enter de forma confiável; a
        confirmação é: caixa de texto vazia, botão de gerar desativado, ou um tile de geração novo
        (`flow-pending-tile` / `flow-error-tile`, que o Flow põe no topo da galeria logo após o
        clique). Nunca reenvia sem antes não haver sinal algum. Não conte o texto "Gerando": é o
        título de vídeos antigos e varia conforme a galeria virtualizada renderiza.
        """
        self.click_step("prompt_box")
        self.page.keyboard.insert_text(prompt)
        time.sleep(2)

        def estado():
            screen = self.screen()
            box = [e for e in screen if e["role"] == "textbox" and e["visible"]]
            try:
                botao = resolve_step("submit", screen)
            except StepError:
                botao = None
            return {"vazio": not "".join(e["text"] for e in box).strip(),
                    "desativado": bool(botao and botao["disabled"]),
                    "pendentes": sum(1 for t in self.page.evaluate(GALLERY) if t["kind"] in ("pending", "error"))}

        antes = estado()
        if antes["vazio"]:
            raise FlowError("ui_changed", "Não consegui escrever o comando na caixa de comando do Flow.")
        self.click_step("submit")
        for tentativa in range(2):
            for _ in range(15):
                time.sleep(1)
                agora = estado()
                if agora["vazio"] or agora["desativado"] or agora["pendentes"] > antes["pendentes"]:
                    return
            if tentativa == 0:  # Nenhum sinal de envio: o clique não pegou, Enter costuma resolver.
                self.click_step("prompt_box")
                self.page.keyboard.press("End")
                self.page.keyboard.press("Enter")
                time.sleep(2)
        raise FlowError("ui_changed", "O Flow não confirmou o envio do comando (nenhum sinal de geração).")

    def counts(self):
        """Estado usado para reconhecer o resultado novo: miniaturas da galeria e cartões de confirmação."""
        return {"tiles": len(self.tile_thumbs()), "bubbles": 0,
                "cards": sum(1 for e in self.screen() if e["role"] == "radio" and re.fullmatch(r"aprovar", e["aria"] or "", re.I)),
                "thumbs": self.tile_thumbs()}

    def card_if_any(self, expected_cost, before=None, timeout=20):
        """Cartão de confirmação, se aparecer. Com o Agente desligado não existe: a geração começa direto.

        Fica como rede de segurança caso o modo Agente ligue sozinho — sem isso o worker geraria
        com os parâmetros errados sem perceber.
        """
        before = before or {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.check_session()
            cards = self.page.evaluate(CARDS)
            text = cards[-1] if cards and (before.get("cards") or 0) <= len(cards) else ""
            if text:
                match = COST.search(text)
                if not match:
                    raise FlowError("ui_changed", "Cartão de confirmação sem custo legível: " + text[:150])
                return int(match.group(1)), text.strip()
            agent = self.agent_text()
            if NO_CREDITS.search(agent):
                raise FlowError("no_credits", "Flow sem créditos suficientes: " + agent[-200:])
            time.sleep(2)
        return None, ""

    def agent_text(self):
        return "\n".join(self.page.evaluate(BUBBLES)[-3:])

    def answer(self, approve):
        """Pick exactly "Aprovar" or "Rejeitar" (never "Sempre aprovar": it would disable the cost check)."""
        step_id = "approve" if approve else "reject"
        option = self.step(step_id, timeout=5)  # `gated`: só a lista fechada de botões passa.
        self.locate(option).click()
        wanted = "aprovar" if approve else "rejeitar"
        for _ in range(20):
            time.sleep(0.5)
            if not any(e["role"] == "radio" and e["checked"] != "true" and not e["disabled"]
                       and (e["aria"] or "").casefold() == wanted for e in self.screen()):
                return
        raise FlowError("ui_changed", f"O Flow não registrou a opção {'Aprovar' if approve else 'Rejeitar'}.",
                        charged=approve)

    def tile_thumbs(self):
        """Miniaturas da galeria (uma por vídeo pronto, em ordem do mais recente para o mais antigo)."""
        return [t["thumb"] for t in self.page.evaluate(GALLERY) if t["kind"] == "video" and t.get("thumb")]

    def gallery(self, image_name):
        return scan_gallery(self.page.evaluate(GALLERY), image_name, self.seen_thumbs)

    def wait_render(self, before, image_name="", timeout=1800):
        """Espera o vídeo DESTA tarefa aparecer na galeria, recarregando o projeto de tempos em tempos.

        O cartão do chat NÃO serve como sinal: vídeo concluído e vídeo 'na fila' têm o mesmo DOM
        (`img.video-thumbnail` é a imagem de origem, não o vídeo). Só vale um vídeo pronto ACIMA da
        imagem enviada nesta tarefa (`scan_gallery`): comparar a galeria inteira com a de antes
        confunde vídeo antigo que a lista virtualizada voltou a renderizar com vídeo novo. Falha aparece
        como `flow-error-tile` no lugar da geração — e some no próximo recarregamento, então ela é
        conferida antes de recarregar, e não se recarrega enquanto a geração está visível em andamento.
        A fila do Flow pode levar mais de 15 min ('alta demanda').
        """
        self.seen_thumbs = set((before or {}).get("thumbs") or [])
        deadline = time.time() + timeout
        last_reload = next_log = time.time()
        while time.time() < deadline:
            self.check_session()
            last = (self.page.evaluate(BUBBLES) or [""])[-1]
            if FAILED_CARD.search(last):
                raise FlowError("render_failed", "Flow informou falha na geração: " + last.strip()[-250:], charged=True)
            state = self.gallery(image_name)
            if state["errors"]:
                raise FlowError("render_failed", "O Flow mostrou falha na geração: " + state["errors"][0][:250],
                                charged=True)
            if state["fresh"]:
                self.new_thumbs = state["fresh"][:1]  # O mais recente acima da imagem: o desta tarefa.
                return
            now = time.time()
            if now >= next_log + 90:
                next_log = now
                if state["pending"]:
                    self.log("Flow: geração desta tarefa em andamento na galeria; aguardando.")
                elif state["anchored"]:
                    self.log("Flow: geração desta tarefa não aparece na galeria; recarregando para conferir.")
                else:
                    self.log(f"Flow: imagem {image_name} não encontrada na galeria; recarregando para conferir.")
            # Em andamento: recarrega só a cada 5 min (recarregar apaga um tile de falha ainda não lido).
            if now - last_reload >= (300 if state["pending"] else 90):
                self.page.goto(self.project_url, wait_until="domcontentloaded", timeout=90000)
                self.step("prompt_box", timeout=60)
                time.sleep(5)
                last_reload = time.time()
                continue
            time.sleep(5)
        raise FlowError("render_timeout", "O vídeo não ficou pronto no prazo; confira o projeto no Flow.", charged=True)

    def download(self, destination):
        """Baixa em 720p o tile detectado por `wait_render`. Sem ele, falha: nunca baixa 'o mais recente'."""
        fresh = list(self.new_thumbs or [])
        tiles = self.page.evaluate(GALLERY)  # também marca cada tile para o snapshot por escopo
        index = next((n for n, t in enumerate(tiles) if t["kind"] == "video" and t.get("thumb") in fresh), -1)
        if not fresh or index < 0:
            raise FlowError("download_failed", "O vídeo desta tarefa sumiu da galeria antes do download; "
                                               "confira o projeto no Flow.", charged=True)
        self.click_step("tile_more", timeout=10, tile=index)
        self.click_step("menu_download", timeout=15)
        size = self.step("menu_size", timeout=15)
        with self.page.expect_download(timeout=180000) as pending:
            self.locate(size).click()
        pending.value.save_as(str(destination))
        return Path(destination)

    def generate(self, project_url, images, prompt, expected_cost, model, before_approve, on_stage,
                 workdir, dry_run=False):
        """`images`: um caminho (vídeo automático) ou a lista de referências do Estúdio, a principal primeiro."""
        images = [images] if isinstance(images, (str, Path)) else list(images)
        if not 1 <= len(images) <= 4:
            raise FlowError("ui_changed", f"{len(images)} imagens de referência; o Flow recebe de 1 a 4.")
        self.open_project(project_url)
        on_stage("preparing")
        cost = self.ensure_settings(model, expected_cost)  # Agente desligado + chave Vídeo + Elementos
        self.new_session()
        self.attach_images(images)
        before = self.counts()
        refs = ("a imagem anexada como referência do produto" if len(images) == 1 else
                f"as {len(images)} imagens anexadas como referência do mesmo produto")
        command = (f"Gere agora exatamente 1 vídeo com o modelo {model}, proporção 9:16, 15 segundos, usando "
                   f"{refs}. Não faça perguntas. {prompt}")
        if dry_run:
            self.log(f"Flow: dry_run; nada enviado ({cost} créditos seriam gastos).")
            return None
        before_approve(cost)  # Com o Agente desligado os créditos saem no envio: avisa o painel antes.
        self.submit(command)
        on_stage("submitted", credits=cost)
        approved, text = self.card_if_any(expected_cost, before)  # Só existe se o Agente estiver ligado.
        if approved is not None:
            self.log(f"Flow pediu confirmação: {text[:160]}")
            if approved != expected_cost or not re.search(r"\b1 gera[cç][aã]o\b", text):
                self.answer(False)
                raise FlowError("cost_mismatch", f"Flow pediu {approved} créditos ('{text[:120]}'); o esperado era "
                                                 f"{expected_cost} para 1 geração. Geração recusada.")
            self.answer(True)
        # Âncora da galeria: a última imagem enviada é a mais nova; só o que está acima dela é desta tarefa.
        self.wait_render(before, Path(images[-1]).name)
        on_stage("downloading")
        return self.download(Path(workdir) / "flow.mp4")
