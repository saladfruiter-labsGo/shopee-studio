# Worker da VM hermes

O painel roda no Unraid em container somente leitura. O Antigravity CLI (`agy`) e o Chromium logado no Google Flow ficam na VM `hermes`. O worker liga os dois: busca tarefas no painel, executa e devolve o resultado.

Dois laços independentes rodam no mesmo serviço: `hermes` (tarefas de modelo) e `hermes.video` (vídeos). Assim, um vídeo em andamento não atrasa a análise diária dos produtos.

## Como funciona

- O worker chama `POST /api/worker/claim` a cada 5 segundos com o token do worker. O painel nunca abre conexão para a VM.
- Cada análise de produto vira uma tarefa `llm`. O worker baixa as imagens do anúncio e dos candidatos para uma pasta temporária, roda `agy -p … --model <modelo> --add-dir <pasta> --output-format json --json-schema …` e devolve a saída estruturada. Ao terminar, apaga a pasta.
- O `agy` roda **sem** `--dangerously-skip-permissions`. Títulos de anúncios são texto de terceiros. Sem essa opção, o modo headless nega ferramentas de terminal: uma instrução maliciosa no título não executa comandos na VM. A leitura das imagens continua funcionando, e isso foi testado na VM.
- Sem contato por 90 segundos, o worker fica offline. O painel pula os modelos `agy` e segue para os fallbacks OpenAI-compatíveis configurados.
- Tarefas têm prazo. Se uma tarefa vencer antes de o worker responder, o resultado atrasado é recusado. O painel não espera tarefas criadas antes de reiniciar.

## Vídeos (Google Flow)

- Perfil do navegador: `~/.local/share/flow-vm-test/profile` (chave `flow_profile` no config). Ele é exclusivo do worker e usa `--no-sandbox`, autorizado para esse perfil. O Chromium é o do Playwright 1.62.0 (`~/.cache/ms-playwright/chromium-1234`), numa tela virtual Xvfb `:95`. Ele abre por tarefa e fecha no fim, para liberar memória para a transcrição.
- Ambiente: `~/.local/share/shopee-studio-worker/venv` (Python 3.12, Playwright, faster-whisper). As versões estão fixadas no `install.sh` para reaproveitar os wheels do cache do uv; o disco da VM é pequeno.
- Transcrição: faster-whisper `small` em CPU int8 (chave `whisper_model`). Usa ~600 MB de RAM e leva ~10 s por vídeo de 10 s.
- Cada etapa é informada ao painel. A etapa `approving` é registrada **antes** do clique em Aprovar; se o painel recusar (fila pausada), o worker escolhe Rejeitar. Na falha, a tela do Flow fica em `~/.cache/shopee-studio-worker/ultimo-erro.png`.
- **Interface nova do Flow (nov/2026):** o projeto abre com uma caixa de comando única, e o modo Agente é um chip desligado por padrão. O worker:
  1. **desliga** o chip **Agente** (`flow-agent-mode-toggle-chip`, `aria-pressed=false`) e confere o estado — é o que libera a chave Imagem/Vídeo no painel do compositor;
  2. ~~em **Configurações do agente**~~ — **não usar**: com o Agente desligado esse painel nem existe;
  3. no painel **Gatilho de configurações** do compositor fixa Vídeo, Elementos, 9:16, 720p, 10 s e lê o custo estimado antes de enviar; custo diferente do esperado em Preferências → `cost_mismatch`, sem gastar crédito.
  Os seletores antigos (`flow-settings-view` aberto por botão `tune`) não existem mais fora do modo Agente — era isso que fazia a fila pausar com "Modelo padrão de vídeo é ''".
- **Modo Agente DESLIGADO é obrigatório.** Só com o Agente desligado o painel do compositor oferece a chave **Imagem / Vídeo**; ligado, o Flow não deixa escolher vídeo e o resultado sai errado. O worker confere o `aria-pressed` do chip (`flow-agent-mode-toggle-chip button` / botão `Agente` da caixa de comando) em toda tarefa e desliga se estiver ligado. Depois abre **Gatilho de configurações** e marca **Vídeo**, **Elementos**, 9:16, 720p, 10 s e x1, conferindo o modelo e o custo.
- **Sem cartão de confirmação.** Com o Agente desligado a geração começa no clique de enviar: o worker avisa o painel (`approving`) **antes** e registra os créditos como gastos ao enviar. `card_if_any()` fica só como rede de segurança por 20 s, caso o Agente ligue sozinho.
- **Detecção do resultado:** o cartão do chat do agente **não** serve como sinal — vídeo concluído e vídeo na fila têm o mesmo DOM (`img.video-thumbnail` é a imagem de origem). A galeria do projeto ("Todas as mídias") é ordenada do mais novo para o mais antigo e **virtualizada**: entre leituras e recarregamentos, vídeos antigos entram e saem do DOM. Por isso o worker só aceita um vídeo pronto **acima da imagem enviada na própria tarefa** (`produto<id>.jpg`, função `scan_gallery`) — comparar a galeria inteira com a de antes do envio fazia um vídeo antigo, re-renderizado, parecer novo, e o Telegram recebeu vídeos de outros produtos (10/11/2026). Logo após o clique o Flow põe no topo um `flow-pending-tile` (com o texto do comando); ele vira o vídeo pronto ou um `flow-error-tile` ("Falha / Não foi possível gerar o vídeo."), que **some ao recarregar** — então a falha é lida antes de cada recarga, que acontece a cada 90 s sem geração visível e a cada 5 min com ela em andamento. Falha → `render_failed` com a mensagem do Flow. O download baixa só o tile detectado; se ele sumir, a tarefa falha (`download_failed`) em vez de baixar "o mais recente". A fila do Flow pode passar de 15 min ("alta demanda") — o prazo é 30 min.
- **Envio confirmado pelo tile de geração:** o envio conta como feito quando a caixa esvazia, o botão `Iniciar geração` fica desativado ou surge um `flow-pending-tile`/`flow-error-tile` novo. Não use a contagem do texto "Gerando" (é título de vídeos antigos) nem o total de `flow-video-tile` (varia com a virtualização).


- **Rótulos do seletor de mídia mudam sem aviso.** Em 12/11/2026 o botão de upload deixou de se chamar "Enviar mídia" e virou **"Fazer upload de mídia"** (texto do botão: `upload Fazer upload de mídia`), e a geração morreu com `Timeout ... has_text="Enviar mídia"` antes de gastar crédito. Por isso `attach_image` procura por **padrão** (`UPLOAD_BUTTON`, texto **ou** `aria-label`) e `overlay_button()` é o único jeito de achar botão de overlay. Nunca volte a fixar o texto exato de uma versão; se um dia falhar, o erro diz qual rótulo procurar e a captura da tela fica em `ultimo-erro.png`.
- **Chamadas na tela (worker 1.4.0):** `finish.callouts` (até 3 textos) vira 3 estilos ASS `Chamada1..3` na faixa de cima (15% da altura), caixa opaca vermelha/amarela/laranja, "pop" de 35%→115%→100% e leve inclinação; janelas 5–30%, 33–60% e 63–100% da duração, a última pulsando junto do selo. `finish.subtitles = false` pula a transcrição e não queima a legenda da fala. Sem essas chaves, nada muda.
- **Estúdio (worker 1.3.0):** a tarefa pode trazer `images` (até 4, a principal primeiro): fotos do anúncio por URL e fotos do usuário por id, baixadas do painel em `GET /api/worker/tasks/<id>/images/<foto>` com o token e o `X-Worker-Id`. Cada referência vira `p<tarefa>r<n>.jpg`; o compositor é fixado em **Elementos** (em Frames, duas imagens viram quadro inicial/final), as imagens são anexadas uma a uma e o worker confere que a caixa tem exatamente N anexos antes de enviar. A âncora da galeria é a **última** imagem enviada. Tarefas sem `images` (vídeo automático) continuam com `produto<tarefa>.jpg`. Validado sem crédito em 12/11/2026 com `generate(..., dry_run=True)`: 2 anexos, custo 15.
- **Modo Moderno (worker 1.2.0):** quando a tarefa traz `finish`, o worker extrai 3 quadros do clipe cru (20%, 50% e 80%, 360 px, JPEG) e os envia a `/api/worker/tasks/<id>/frames` para a conferência de fidelidade; depois renderiza num único ffmpeg em 1080×1920, 30 fps (fundo desfocado se a proporção não for 9:16), com legenda, selo de compra nos últimos 3,5 s e áudio `loudnorm`/AAC. Falha ao mandar os quadros só fica no log — o vídeo segue. Tarefas sem `finish` (Modo Legacy) saem como antes.

## Padrão v3: edição em duas etapas (worker 1.6.0, issue #14)

Quando o payload da tarefa de vídeo traz o bloco `edit`, o worker **não** legenda nem põe selo ou texto sobre o vídeo (ED-6). A edição acontece em **duas etapas**, porque os cortes dependem de visão e o worker não tem visão:

**Etapa 1 (tarefa de geração).** O Flow gera o clipe bruto (custo esperado e `video_daily_credits` vêm da config; padrão 0 = nenhuma geração real). `deliver_raw` copia o bruto para `<clips_dir>/raw/video_<id>_bruto.mp4` e registra no SQLite `<clips_dir>/clips.sqlite` (ED-13); o worker extrai os quadros (a cada 0,5 s, em `<clips_dir>/frames/video_<id>_bruto/`) e deixa o clipe em **`aguardando_cortes`** (no painel: "aguardando_cortes (nada enviado)"). **Nada é editado nem enviado.** Sem cortes, isso não é falha.

**Rotina do agente de visão.** Olha os quadros, escolhe os cortes e escreve a nota de fidelidade, e manda tudo ao painel (mesmo token do worker):

    GET  /api/worker/clips/<id>        # estado, ficha e cena de uso do vídeo
    POST /api/worker/clips/<id>/edit   {"cuts": [6.4, 8.5, 10.6], "fidelity_note": {...}, "strategy": "uso_real_trajeto"}   # 202

`cuts`: lista crescente de 1 a 12 tempos (s). `fidelity_note`: cor por parte, ação das partes móveis, trajeto e destino (`v3.fidelity_issues`). O POST cria a **tarefa de edição** na fila do worker (`edit_stage`). Passou de `edit_cuts_timeout_h` horas (config, padrão 24) sem cortes: o vídeo vira **"falha na edição, nada enviado"** (`edit_failed`) e não aceita mais cortes.

**Etapa 2 (tarefa de edição).** `edit_clip` corta com ffmpeg em 10–12 s e mantém o áudio nativo (os cortes são checados antes do whisper). Depois o **editado** passa pela conferência de fidelidade: sem nota, ou com qualquer issue, o status vira **`reprovado_fidelidade`** ("reprovado na fidelidade, nada enviado"), bruto e editado ficam guardados e **nada é enviado**. Só com a conferência aprovada o editado sobe ao painel e segue para o Telegram (o painel também recusa um editado sem `fidelity_ok`). Falha na edição (ED-11, inclusive sem transcrição por palavra, ED-8): o bruto fica, `edit_failed`, crédito 0 do SQLite do worker, nada enviado; o painel mostra "falha na edição, nada enviado", não "incerto".

Quando a análise não tem v3 (fallback do modelo, ou o corte do Estúdio mandando o produto para o automático), o painel registra o vídeo como `skipped: roteiro v3 indisponível` e não cria tarefa nem gasta crédito. O **Estúdio ("Do dia" e manual) está desativado no padrão v3** ("Estúdio desativado no padrão v3.").

Bloco `edit` do payload: `product_key`, `ficha` e `use_scene` (do roteirista v3) e `config` (parâmetros da edição, vindos da configuração do painel: `edit_min_s`, `edit_max_s`, `hook_end_s`, `shot_target_s`, `shot_tol_s`, `static_max_s`, `word_margin_s`, `lead_silence_max_s` e `edit_tolerance_s`). Na etapa 2 ele ganha `cuts`, `fidelity_note` e `strategy` (V3-8, gravados no SQLite).

**Faixas e custo.** Fala de 15 s com a voz nativa do Flow: a narração segue `script_words_target` ± `script_words_tol` (padrão 25 ± 3 = 22–28 palavras; as faixas antigas 18–25 e 14–19 saíram do automático) e **termina por volta de 11 s**; o editado fica em **10–12 s** (decisão sobre o conflito 10–12 s × 15 s: a fala acaba até ~11 s, então o corte final cabe sem cortar palavra). A tolerância de duração do editado é o parâmetro `edit_tolerance_s` (padrão 0,15 s, ED-1), não um literal. O custo esperado é `video_clip_cost_expected` (padrão 60; `video_cost` continua como apelido) e `video_daily_credits` padrão 0 recusa toda geração real, de qualquer origem. Bancos antigos (15/75) migram uma vez para 60/0.

**Quem orquestra a visão (a rotina do agente).** A rotina extrai/olha os quadros (`video_edit.py frames --raw X --out D` ou os de `frames_dir`), decide os cortes (ED-9) e a nota de fidelidade e chama o POST acima; fora do painel, a CLI `video_edit.py edit --cuts cortes.json`. **Sem `cuts` a edição recusa com `cuts_missing` (exit 2, antes do whisper, estado e bruto intactos): o módulo não inventa cortes de 2 s (ED-3/ED-9).** Plano parado no meio do clipe não derruba a edição: o tempo morto além de `static_max_s` é retirado por concat (ED-4), sem cortar palavra. O portão de fidelidade também existe como `video_edit.py fidelity --db DB --video-id N --ficha F.json --script S.json [--frames-out D]` (saída validada por schema; fora dele: JSON de erro `invalid_result`, exit 8). A nota nunca é gerada pelo worker.

**Transcrição.** `faster-whisper` está em `worker/requirements.txt` (mesmas versões do `install.sh`). Quem instala é o `install.sh`, depois do clique do igaoeu; sem ele a transcrição falha, a edição não roda e nada é enviado. Com fala densa, um plano pode passar da faixa de 1,5–2,5 s (ex.: 2,62 s): o ED-8 (não cortar palavra) prevalece sobre o ED-3 e o aviso fica registrado em `warnings`.

**Dry run (0 crédito, sem Flow, sem painel).**

    python3 worker/studio_worker.py --dry-run-edit --workdir /tmp/dry            # gera clipe de fixture com ffmpeg
    python3 worker/studio_worker.py --dry-run-edit --stage1-only --workdir /tmp/dry   # só a etapa 1: aguardando_cortes, nada enviado
    python3 worker/studio_worker.py --dry-run-edit --clip X.mp4 --words X.words.json --cuts cortes.json --review review.json --workdir /tmp/dry   # --cuts e --review (ficha, use_scene, fidelity_note) são obrigatórios com clipe próprio

A fixture roda a etapa 1 e a 2 com cortes e nota próprios. Imprime um JSON (`ok`, `credits`=0, `raw_path`, `edited_path`, `duration`, `cuts`, `warnings`) e sai com 0, ou com o código de `video_edit.py` (2, 3, 4, 5, 6, 7; 8 = resultado fora do schema, com JSON de erro próprio; 9 = fidelidade incompleta) em falha.

### Login, 2FA ou CAPTCHA

Quando o Google pede login ou verificação, a fila pausa e chega um alerta no Telegram. Na VM:

    ~/.local/share/shopee-studio-worker/flow_login.sh        # ou: worker/flow_login.sh pc1-lan

O script para o worker e abre o navegador do perfil numa tela virtual. Ele também expõe a tela no PC por noVNC, através de um túnel SSH reverso: no PC, abra http://127.0.0.1:6086/vnc.html. Nada fica exposto na LAN. Faça o login e tecle Ctrl+C no terminal; o script fecha tudo e reinicia o worker. Depois, toque em **Retomar vídeos** no painel.

## Instalação (uma vez)

1. No painel: Preferências → Worker da VM → **Gerar novo token**. Copie o token.
2. Na VM `hermes`, dentro do checkout do repositório:

       worker/install.sh http://192.168.1.90:8787

   Cole o token quando o script pedir. Ele fica em `~/.config/shopee-studio-worker/config.json` com modo 600.
3. Para o serviço continuar após o logout e voltar após um reboot, o usuário precisa de linger: `loginctl enable-linger hermes`.

Atualizar o worker é rodar `worker/install.sh` de novo. O script preserva o config, instala as dependências de vídeo, baixa o modelo de transcrição (~460 MB na primeira vez) e reinicia o serviço.

## Operação

    systemctl --user status shopee-studio-worker
    journalctl --user -u shopee-studio-worker -f
    systemctl --user stop shopee-studio-worker     # o painel passa a usar os fallbacks

Trocar o token no painel desconecta o worker. Depois, edite `config.json` com o token novo e reinicie o serviço.
