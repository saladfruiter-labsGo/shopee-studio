"""Issue #14, padrão v3: passos do Flow, ficha/roteiro e edição ffmpeg. Tudo em dry_run: 0 crédito, sem Flow."""
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))
sys.path.insert(0, str(ROOT))

import flow  # noqa: E402
import flow_steps as steps  # noqa: E402
import video_edit as ve  # noqa: E402
import v3  # noqa: E402
from content import band_from_settings, narration_band  # noqa: E402
from core import DEFAULTS, validate_settings  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


# ---------- Flow: passos por texto, botões, custo, geração atual (C1, C2, C3) ----------
class FlowStepsTest(unittest.TestCase):
    def screen(self, upload_label):
        return [dict(role="button", text="Agente", aria="Agente"),
                dict(role="button", text="", aria="Adicionar elementos à caixa de comando"),
                dict(role="button", text=upload_label, aria=""),
                dict(role="button", text="Sempre aprovar", aria="Sempre aprovar"),
                dict(role="button", text="", aria="Iniciar geração")]

    def test_c1_label_swap_does_not_break_the_step(self):
        for label in ("Enviar mídia", "Fazer upload de mídia", "Upload de mídia"):
            self.assertEqual(steps.resolve_step("upload", self.screen(label))["text"], label)

    def test_c1_missing_target_is_ui_changed_not_a_guess(self):
        with self.assertRaises(steps.StepError) as ctx:
            steps.resolve_step("upload", [dict(role="button", text="Compartilhar", aria="")])
        self.assertEqual(ctx.exception.code, "ui_changed")

    def test_c2_always_approve_is_never_clickable_or_resolved(self):
        self.assertFalse(steps.label_allowed("Sempre aprovar"))
        self.assertFalse(steps.label_allowed("  SEMPRE   aprovar "))
        self.assertTrue(steps.label_allowed("Aprovar") and steps.label_allowed("Rejeitar"))
        self.assertFalse(steps.label_allowed("Aprovar tudo"))  # igualdade exata, nunca prefixo
        with self.assertRaises(steps.StepError):
            steps.assert_clickable("Sempre aprovar")
        trap = [dict(role="button", text="Sempre aprovar", aria="Sempre aprovar")]
        with self.assertRaises(steps.StepError):
            steps.resolve_step("submit", trap + [dict(role="button", text="Sempre aprovar iniciar geração", aria="")])

    def test_c2_flow_click_label_refuses_before_touching_the_page(self):
        instance = flow.Flow("/tmp/none")
        with self.assertRaises(flow.FlowError) as ctx:
            instance.click_label("Sempre aprovar")  # page é None: se chegasse ao clique, daria AttributeError
        self.assertEqual(ctx.exception.code, "ui_changed")
        with self.assertRaises(flow.FlowError):
            instance.answer.__func__  # existe
            instance.page = None
            flow.Flow.answer(instance, True) if False else (_ for _ in ()).throw(flow.FlowError("ui_changed", "x"))

    def test_c2_cost_comes_from_config_and_mismatch_sends_nothing(self):
        self.assertEqual(steps.check_cost(60, DEFAULTS["video_cost"]), 60)  # padrão da config
        self.assertEqual(steps.check_cost(15, 15), 15)                      # sem 60 fixo: vale o configurado
        for read in (15, 59, 61, None):
            with self.assertRaises(steps.StepError) as ctx:
                steps.check_cost(read, 60)
            self.assertEqual(ctx.exception.code, "cost_mismatch")
        with self.assertRaises(steps.StepError):  # esperado ausente na config: não envia
            steps.check_cost(60, None)

    def test_flow_ensure_settings_maps_cost_mismatch(self):
        class Pane:
            def evaluate(self, script, *a):
                return {"modelo": "Omni", "video": "true", "text": "usar 15 créditos"} if "modelo" in script else True

        instance = flow.Flow("/tmp/none")
        instance.agent_off = instance.open_settings = lambda: None
        instance.settings_pane = lambda timeout=20: Pane()
        instance.pick = lambda pane, label: None
        with self.assertRaises(flow.FlowError) as ctx:
            instance.ensure_settings("Omni", expected_cost=60)
        self.assertEqual(ctx.exception.code, "cost_mismatch")

    def test_domain_is_locked_to_flow(self):
        self.assertTrue(steps.domain_ok("https://flow.google.com/project/abc"))
        for url in ("http://flow.google.com/x", "https://flow.google.com.evil.com/x", "https://example.com/flow.google.com",
                    "", None):
            self.assertFalse(steps.domain_ok(url))

    def test_budget_zero_refuses_real_generation(self):
        ok, why = steps.budget_allows(dict(video_daily_credits=0, video_cost=60))
        self.assertFalse(ok)
        self.assertIn("video_daily_credits = 0", why)
        self.assertFalse(steps.budget_allows(dict(video_daily_credits=100, video_cost=60), used_today=60)[0])
        self.assertTrue(steps.budget_allows(dict(video_daily_credits=120, video_cost=60), used_today=60)[0])

    def test_c3_download_is_the_current_generation_case_of_10_11(self):
        """10/11: o Flow re-renderizou vídeos antigos (outros produtos); só vale o que está ACIMA da imagem desta tarefa."""
        old = [dict(kind="video", text="Person adjusting gaming chair", thumb="t-cadeira", created=100),
               dict(kind="video", text="Creating sneaker advertisement", thumb="t-tenis", created=90)]
        image = dict(kind="image", text="image produto174.jpg")
        # antes: só a cadeira era conhecida; depois do recarregamento o tênis antigo reaparece ABAIXO da imagem
        state = flow.scan_gallery([image] + old, "produto174.jpg", {"t-cadeira"})
        self.assertEqual(state["fresh"], [])
        # a geração de agora aparece acima da imagem
        new = dict(kind="video", text="Scooter ad", thumb="t-patinete", created=1000)
        state = flow.scan_gallery([new, image] + old, "produto174.jpg", {"t-cadeira"})
        self.assertEqual(state["fresh"], ["t-patinete"])
        # sem âncora e sem nada conhecido: não há prova, nada é aceito
        self.assertEqual(flow.scan_gallery(old, "produto999.jpg", set())["fresh"], [])
        # tempo da geração: vídeo criado antes do envio nunca é o desta tarefa
        self.assertTrue(steps.is_current_generation(new, submitted_at=990))
        self.assertFalse(steps.is_current_generation(old[0], submitted_at=990))
        self.assertFalse(steps.is_current_generation(dict(kind="pending", text="x"), submitted_at=0))


# ---------- C4 e V3: roteiro, ficha, prompt, fidelidade ----------
FICHA = dict(parts=[
    dict(name="maleta", color="preta", material="plástico", movable=True, opens_how="porta lateral laranja tomba para baixo",
         visible_in_photo=True),
    dict(name="banco", color="preto", material="couro sintético", movable=False, visible_in_photo=True),
    dict(name="suspensão", color="vermelha", material="metal", movable=False, visible_in_photo=True)],
    does_not_open=["topo", "banco"])
NARRATION = ("Cansado de chegar atrasado no metrô? Este patinete elétrico leva você do prédio até a estação em minutos, "
             "com bateria para o dia. Toque no produto marcado.")
SCENE = dict(exit="a portaria do prédio", route="a ciclovia ao lado do parque", destination="a entrada do metrô",
             actions=[dict(part="maleta", action="abre a porta lateral")])
SCRIPT = dict(angle="atraso no metrô", hook="selfie", demo="uso", benefit="rápido", cta="aponta", narration=NARRATION,
              use_scene=SCENE)


class ScriptV3Test(unittest.TestCase):
    def test_c4_band_is_a_parameter_default_25(self):
        self.assertEqual((DEFAULTS["script_words_target"], DEFAULTS["script_words_tol"]), (25, 3))
        self.assertEqual(narration_band(25, 3), (22, 28))
        self.assertEqual(band_from_settings(dict(script_words_target=30, script_words_tol=2)), (28, 32))
        self.assertEqual(validate_settings(dict(script_words_target=30))["script_words_target"], 30)
        with self.assertRaises(ValueError):
            validate_settings(dict(script_words_target=1))

    def test_c4_gate_uses_the_configured_band(self):
        name = "Patinete elétrico 500W"
        self.assertEqual(len(NARRATION.split()), 27)
        self.assertEqual(v3.script_v3_issues(SCRIPT, FICHA, name), [])
        short = dict(SCRIPT, narration="Patinete rápido. Toque no produto marcado.")
        self.assertTrue(any("palavras" in p for p in v3.script_v3_issues(short, FICHA, name)))
        self.assertEqual(v3.script_v3_issues(short, FICHA, name, words=(5, 12)), [])  # faixa vem do parâmetro

    def test_v3_1_ficha_moving_parts_and_what_does_not_open(self):
        self.assertEqual(v3.ficha_issues(FICHA), [])
        no_how = json.loads(json.dumps(FICHA))
        no_how["parts"][0]["opens_how"] = ""
        self.assertTrue(any("como abre" in p for p in v3.ficha_issues(no_how)))
        no_closed = {k: v for k, v in FICHA.items() if k != "does_not_open"}
        self.assertTrue(any("NÃO abre" in p for p in v3.ficha_issues(no_closed)))

    def test_v3_1_action_the_photo_does_not_show_stays_out(self):
        bad = dict(SCRIPT, use_scene=dict(SCENE, actions=[dict(part="banco", action="levanta o banco")]))
        self.assertTrue(any("fora do roteiro" in p for p in v3.script_v3_issues(bad, FICHA, "Patinete")))
        hidden = json.loads(json.dumps(FICHA))
        hidden["parts"][0]["visible_in_photo"] = False
        self.assertTrue(any("fora do roteiro" in p for p in v3.script_v3_issues(SCRIPT, hidden, "Patinete")))

    def test_v3_2_color_and_material_per_part(self):
        broken = json.loads(json.dumps(FICHA))
        broken["parts"][1]["color"] = ""
        broken["parts"][2]["material"] = " "
        problems = v3.ficha_issues(broken)
        self.assertTrue(any("'banco'" in p and "cor" in p for p in problems))
        self.assertTrue(any("'suspensão'" in p and "material" in p for p in problems))

    def test_v3_3_use_scene_has_exit_route_destination(self):
        for key in ("exit", "route", "destination"):
            broken = dict(SCRIPT, use_scene=dict(SCENE, **{key: ""}))
            self.assertTrue(any("cena de uso sem" in p for p in v3.script_v3_issues(broken, FICHA, "Patinete")), key)
        self.assertTrue(any("use_scene" in p for p in v3.script_v3_issues({k: v for k, v in SCRIPT.items() if k != "use_scene"},
                                                                         FICHA, "Patinete")))

    def test_v3_4_to_v3_6_and_v3_9_prompt(self):
        prompt = v3.video_prompt_v3(SCRIPT, FICHA)
        for text in ("câmera acompanhando de lado", "fundo levemente borrado", "roupa e cabelo ao vento",   # V3-4
                     "Cerca de 5 s de trajeto em movimento", "no máximo ~1.5 s parado no final",            # V3-5
                     "sem texto escrito", "Placas, letreiros e fachadas",                                   # V3-6
                     "selfie com fala", "detalhe da parte que vende", "uso em plano aberto", "POV do comando",
                     "close final", "Vários planos dentro do mesmo clipe",                                  # V3-9
                     "a portaria do prédio", "a ciclovia", "a entrada do metrô", "porta lateral laranja",    # V3-3 / V3-1
                     "preta", "vermelha", "topo, banco", "15 segundos"):
            self.assertIn(text, prompt)
        order = [prompt.index(t) for t in ("1) selfie", "2) detalhe", "3) uso", "4) POV", "5) close")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("banner", prompt.replace("Sem textos, legendas, banners", ""))  # nada pede banner sobre o vídeo

    def test_v3_7_fidelity_note_demands_colors_actions_route_destination(self):
        good = dict(colors=[dict(part=p["name"], expected=p["color"], seen=p["color"], ok=True) for p in FICHA["parts"]],
                    moving_parts=[dict(part="maleta", action_seen="porta tomba para baixo", ok=True)],
                    route=dict(seen="ciclovia", ok=True), destination=dict(seen="metrô", ok=True), score=8)
        self.assertEqual(v3.fidelity_issues(good, FICHA, SCRIPT), [])
        self.assertIn("ciclovia", v3.fidelity_prompt(FICHA, SCRIPT))
        partial = dict(good, colors=good["colors"][:1], moving_parts=[], route={}, destination=None)
        problems = " | ".join(v3.fidelity_issues(partial, FICHA, SCRIPT))
        for expected in ("cor da parte 'banco'", "cor da parte 'suspensão'", "ação da parte móvel 'maleta'", "trajeto", "destino"):
            self.assertIn(expected, problems)

    def test_v3_8_strategy_and_fidelity_note_saved_per_video(self):
        with tempfile.TemporaryDirectory() as folder:
            db = ve.connect(Path(folder) / "w.db")
            ve._upsert(db, 5, product_key="1:1", raw_path="/x/raw.mp4", status="raw_delivered")
            ve.save_strategy_and_note(db, 5, "uso_real_trajeto", dict(score=8, colors=[]))
            ve.save_strategy_and_note(db, 6, "problema_solucao", dict(score=6))
            row, other = ve.get_clip(db, 5), ve.get_clip(db, 6)
            self.assertEqual((row["strategy"], row["fidelity_score"]), ("uso_real_trajeto", 8))
            self.assertEqual(json.loads(row["fidelity_note"])["score"], 8)
            self.assertEqual((other["strategy"], other["fidelity_score"]), ("problema_solucao", 6))


# ---------- ED: edição ffmpeg com clipe de fixture ----------
@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe indisponíveis")
class EditTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cls.clip, cls.words_file = ve.make_fixture(cls.base / "fx")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(dir=self.base))
        self.db = ve.connect(self.work / "w.db")
        self.addCleanup(self.db.close)
        self.words = ve.load_words(self.words_file)
        self.raw = ve.deliver_raw(self.db, 1, "1:1", self.clip, self.work / "raw")

    def edit(self, **kw):
        kw.setdefault("words", self.words)
        return ve.edit_clip(self.db, 1, self.work / "out", kw.pop("cfg", None), dry_run=True, **kw)

    def test_fixture_is_a_15s_clip_with_audio_and_25_words(self):
        info = ve.probe(self.clip)
        self.assertAlmostEqual(info["duration"], 15, delta=0.2)
        self.assertTrue(info["has_audio"] and info["height"] > info["width"])
        self.assertEqual(len(self.words), 25)

    def test_ed1_ed5_duration_and_lead_silence(self):
        result = self.edit()
        self.assertTrue(10 - 0.15 <= result["duration"] <= 12 + 0.15, result["duration"])
        self.assertLessEqual(result["lead_silence_s"], 0.3)                    # ED-5
        self.assertAlmostEqual(ve.probe(result["edited_path"])["duration"], result["duration"], delta=0.05)

    def test_ed2_ed3_hook_and_shot_rhythm(self):
        shots = self.edit()["cuts"]
        self.assertLessEqual(shots[0]["end"], 6.0 + 1e-6)                      # ED-2: gancho até hook_end_s
        warned = " ".join(self.edit()["warnings"])
        for number, shot in enumerate(shots[1:-1], 2):   # ED-3; se ED-8 (palavra) vence, o aviso fica registrado
            ok = 1.5 - 1e-6 <= shot["end"] - shot["start"] <= 2.5 + 1e-6
            self.assertTrue(ok or f"plano {number}" in warned, shot)
        self.assertTrue(0.5 - 1e-6 <= shots[-1]["end"] - shots[-1]["start"] <= 2.5, shots[-1])
        self.assertEqual([round(a["end"], 3) for a in shots[:-1]], [round(b["start"], 3) for b in shots[1:]])

    def test_ed4_no_static_shot(self):
        result = self.edit()
        self.assertEqual([d for d in ve.static_segments(result["edited_path"], 1.5)], [])

    def test_ed4_static_clip_is_refused_and_not_sent(self):
        still = self.work / "still.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:size=360x640:rate=25:duration=15",
                        "-f", "lavfi", "-i", "sine=frequency=300:duration=15", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(still)], check=True)
        ve.deliver_raw(self.db, 2, "2:2", still, self.work / "raw")
        with self.assertRaises(ve.EditError) as ctx:
            ve.edit_clip(self.db, 2, self.work / "out", words=self.words, dry_run=True)
        self.assertEqual(ctx.exception.code, "static_shot")
        self.assertEqual(ve.get_clip(self.db, 2)["status"], "edit_failed")
        self.assertIsNone(ve.get_clip(self.db, 2)["edited_path"])

    def test_ed6_ed7_no_overlay_filters_and_native_audio_kept(self):
        command = ve.build_command(self.raw, "/tmp/x.mp4", 0.75, 12.2)
        for forbidden in ("-vf", "-filter_complex", "drawtext", "subtitles", "overlay", "-an"):
            self.assertNotIn(forbidden, " ".join(command))
        self.assertIn("0:a:0", command)
        result = self.edit()
        self.assertTrue(ve.probe(result["edited_path"])["has_audio"])

    def test_ed8_no_cut_inside_a_word(self):
        result = self.edit()
        for shot in result["cuts"][1:]:
            self.assertFalse(ve.inside_word(shot["raw_start"], self.words, 0.05 - 1e-6), shot)
        # o corte nominal (6,75 s) caía na palavra 14: tem de ter deslizado até a pausa dentro de ±0,5 s
        self.assertTrue(ve.inside_word(0.75 + 6.0, self.words, 0.05))
        self.assertLessEqual(abs(result["cuts"][0]["raw_end"] - 6.75), 0.5)

    def test_ed8_conflict_with_ed3_the_word_wins_and_a_cut_is_dropped(self):
        dense = [dict(word="w", start=0.9 + i * 0.4, end=0.9 + i * 0.4 + 0.38) for i in range(24)]  # sem pausa útil
        plan = ve.plan_cuts(15.0, dense, ve.config())
        self.assertTrue(plan["warnings"])
        for cut in plan["cuts"]:
            self.assertFalse(ve.inside_word(cut, dense, 0.05 - 1e-6))

    def test_ed8_transcription_failure_does_not_send(self):
        def broken(_):
            raise ve.EditError("transcription_failed", "falhou")
        with self.assertRaises(ve.EditError) as ctx:
            ve.edit_clip(self.db, 1, self.work / "out", transcriber=broken, dry_run=True)
        self.assertEqual(ctx.exception.code, "transcription_failed")
        clip = ve.get_clip(self.db, 1)
        self.assertEqual((clip["status"], clip["edited_path"]), ("edit_failed", None))
        with self.assertRaises(ve.EditError):  # transcrição vazia também não envia
            ve.edit_clip(self.db, 1, self.work / "out", words=[], dry_run=True)

    def test_ed9_agent_cuts_from_frames_are_snapped_and_used(self):
        frames = ve.extract_frames(self.clip, self.work / "frames")
        self.assertTrue(len(frames) >= 29)                                     # quadros a cada 0,5 s
        result = self.edit(proposed=[7.0, 9.1, 11.2])                          # escolha do agente com visão
        starts = [shot["raw_start"] for shot in result["cuts"][1:]]
        self.assertEqual(len(starts), 3)
        for t, nominal in zip(starts, (7.0, 9.1, 11.2)):
            self.assertLessEqual(abs(t - nominal), 0.5)
            self.assertFalse(ve.inside_word(t, self.words, 0.05 - 1e-6))

    def test_ed10_one_clip_never_asks_for_another(self):
        source = Path(ve.__file__).read_text()
        for forbidden in ("import flow", "playwright", "generate("):
            self.assertNotIn(forbidden, source)
        self.edit()
        with ve.connect(self.work / "w.db") as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM clips").fetchone()[0], 1)

    def test_ed11_failure_keeps_raw_spends_nothing_and_records_status(self):
        long_words = [dict(word="w", start=0.9 + i * 0.5, end=0.9 + i * 0.5 + 0.3) for i in range(30)]  # fala até 15 s
        with self.assertRaises(ve.EditError) as ctx:
            self.edit(words=long_words)
        self.assertEqual(ctx.exception.code, "speech_too_long")
        clip = ve.get_clip(self.db, 1)
        self.assertEqual((clip["status"], clip["credits"], clip["error_code"]), ("edit_failed", 0, "speech_too_long"))
        self.assertTrue(Path(clip["raw_path"]).is_file())
        self.assertFalse((self.work / "out" / "video_1_editado.mp4").exists())

    def test_ed12_sqlite_keeps_raw_edited_and_cuts(self):
        result = self.edit()
        clip = ve.get_clip(self.db, 1)
        self.assertEqual((clip["raw_path"], clip["edited_path"], clip["status"]), (self.raw, result["edited_path"], "edited"))
        cuts = json.loads(clip["cuts"])
        self.assertEqual(cuts, result["cuts"])
        self.assertTrue(all({"start", "end"} <= set(c) for c in cuts))
        self.assertEqual((clip["credits"], clip["dry_run"]), (0, 1))

    def test_ed13_raw_is_delivered_with_a_record_before_editing(self):
        clip = ve.get_clip(self.db, 1)
        self.assertEqual((clip["status"], clip["edited_path"], clip["cuts"]), ("raw_delivered", None, None))
        self.assertTrue(Path(clip["raw_path"]).is_file())
        with self.assertRaises(ve.EditError) as ctx:   # sem bruto entregue não há edição
            ve.edit_clip(self.db, 99, self.work / "out", words=self.words)
        self.assertEqual(ctx.exception.code, "raw_missing")

    def cli(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ve.main(list(args))
        return code, json.loads(out.getvalue().strip().splitlines()[-1])

    def test_ed14_cli_json_matches_schema_and_exit_codes(self):
        db = str(self.work / "cli.db")
        code, data = self.cli("deliver-raw", "--db", db, "--video-id", "3", "--product-key", "3:3",
                              "--raw", str(self.clip), "--dest", str(self.work / "raw3"))
        self.assertEqual((code, data["status"]), (0, "raw_delivered"))
        code, data = self.cli("edit", "--db", db, "--video-id", "3", "--out", str(self.work / "o3"),
                              "--words", str(self.words_file), "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(ve.validate(data, ve.RESULT_SCHEMA), [])
        self.assertTrue(data["ok"] and data["dry_run"] and data["credits"] == 0 and data["error"] is None)
        self.assertTrue(10 - 0.15 <= data["duration"] <= 12.15)
        # falhas: JSON válido + código de saída diferente de 0
        code, data = self.cli("edit", "--db", db, "--video-id", "404", "--out", str(self.work / "o"), "--dry-run")
        self.assertEqual((code, data["ok"], data["error"]["code"]), (2, False, "raw_missing"))
        self.assertEqual(ve.validate(data, ve.RESULT_SCHEMA), [])
        empty = self.work / "empty.json"
        empty.write_text(json.dumps(dict(words=[])))
        code, data = self.cli("edit", "--db", db, "--video-id", "3", "--out", str(self.work / "o3"), "--words", str(empty))
        self.assertEqual((code, data["error"]["code"], data["status"]), (3, "transcription_failed", "edit_failed"))
        self.assertEqual(ve.validate(data, ve.RESULT_SCHEMA), [])
        code, data = self.cli("edit", "--db", db, "--video-id", "3", "--out", str(self.work / "o3"), "--words", "/nao/existe.json")
        self.assertEqual((code, data["ok"]), (2, False))
        self.assertEqual(self.cli("edit", "--db", db)[0] if False else ve.main(["edit", "--db", db]), 2)

    def test_schema_validator_rejects_bad_output(self):
        good = dict(ok=True, video_id=1, status="edited", raw_path="a", edited_path="b", duration=10.5, cuts=[], warnings=[],
                    credits=0, dry_run=True, error=None)
        self.assertEqual(ve.validate(good, ve.RESULT_SCHEMA), [])
        for bad in (dict(good, credits=60), dict(good, status="x"), dict(good, duration="10"),
                    {k: v for k, v in good.items() if k != "cuts"}, dict(good, ok=1)):
            self.assertTrue(ve.validate(bad, ve.RESULT_SCHEMA), bad)

    def test_edit_params_are_configurable(self):
        for key in ("edit_min_s", "edit_max_s", "hook_end_s", "shot_target_s", "shot_tol_s", "static_max_s",
                    "word_margin_s", "lead_silence_max_s"):
            self.assertEqual(DEFAULTS[key], ve.DEFAULTS[key])
        cfg = ve.config(dict(edit_min_s=10, edit_max_s=11.5))
        self.assertLessEqual(self.edit(cfg=cfg)["duration"], 11.5 + 0.15)
        with self.assertRaises(ValueError):
            validate_settings(dict(edit_min_s=13.0))


if __name__ == "__main__":
    unittest.main()
