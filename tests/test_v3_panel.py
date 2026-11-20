"""Issue #14 (PR do review do Fronteiro): edição em duas etapas, fallback sem v3, fidelidade segurando o envio e Estúdio
recusado. Painel real (Flask test client), worker falso, sem Flow, sem rede e com 0 crédito real."""
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import analysis  # noqa: E402
import studio as studio_module  # noqa: E402
from app import create_app  # noqa: E402
from test_studio import FakeRunner, node  # noqa: E402

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048
PROJECT = "https://flow.google.com/project/test-project"
NOTE = dict(colors=[dict(part="tampa", seen="branca", ok=True)], moving_parts=[dict(part="tampa", ok=True)],
            route=dict(seen="cozinha", ok=True), destination=dict(seen="geladeira", ok=True), score=8)


def run_inline(target, args, daemon):
    return type("T", (), {"start": lambda self: target(*args)})()


class PanelV3Test(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        sleeper = patch("core.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)
        with patch.dict(os.environ, {"PANEL_PASSWORD": "test-password-only"}):
            self.app = create_app(self.temp.name, testing=True)
        self.client = self.app.test_client()
        self.store, self.engine = self.app.extensions["store"], self.app.extensions["engine"]
        self.engine.fetcher = lambda _: [node(i, imageUrl=f"https://down-br.img.susercontent.com/file/{i}") for i in range(1, 2)]
        self.engine.searcher = lambda *a: []
        self.engine.runner = FakeRunner()
        self.sent = []
        self.engine.sender = self.sender
        self.store.save_telegram(dict(token="123456:" + "A" * 30, chat_id="-1001234", thread_id="7"))
        self.store.save_settings(dict(top_count=1, per_shop=1, flow_project_url=PROJECT,
                                      video_daily_credits=75, video_clip_cost_expected=15))  # teste: orçamento explícito
        self.worker = {"Authorization": f"Bearer {self.store.set_worker_token()}"}

    def sender(self, url, body, headers=None):
        self.sent.append(url.rsplit("/", 1)[1])
        return {"ok": True, "result": {"message_id": 100 + len(self.sent)}}

    def video(self):
        with self.store.db() as db:
            return dict(db.execute("SELECT * FROM videos ORDER BY id DESC LIMIT 1").fetchone())

    def tasks(self):
        with self.store.db() as db:
            return db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]

    def claim(self):
        return self.client.post("/api/worker/claim", json=dict(worker_id="vm.video", kinds=["video"]), headers=self.worker).json["task"]

    def result(self, task, **body):
        with patch("video.threading.Thread") as thread:
            thread.side_effect = run_inline
            return self.client.post(f"/api/worker/tasks/{task['id']}/result", json=dict(worker_id="vm.video", **body),
                                    headers=self.worker)

    def cuts(self, video_id, **body):
        body = dict(dict(cuts=[6.4, 8.5, 10.6], fidelity_note=NOTE, strategy="uso_real_trajeto"), **body)
        return self.client.post(f"/api/worker/clips/{video_id}/edit", json=body, headers=self.worker)

    def awaiting(self):
        """Etapa 1: a geração do worker termina com o bruto guardado e sem enviar nada."""
        run_id = self.engine.collect()
        self.engine.send(run_id)
        task = self.claim()
        self.client.post(f"/api/worker/tasks/{task['id']}/progress",
                         json=dict(worker_id="vm.video", stage="approving", credits=15), headers=self.worker)
        self.result(task, ok=True, output=dict(awaiting_cuts=True, raw_path="/clips/raw/video_1_bruto.mp4", frames=20))
        return task

    def stage2_task(self):
        self.assertEqual(self.cuts(self.video()["id"]).status_code, 202)
        task = self.claim()
        self.assertTrue(task["payload"]["edit_stage"])
        return task

    # ---- item 1: edição em duas etapas ----
    def test_item1_stage1_waits_for_cuts_and_sends_nothing(self):
        task = self.awaiting()
        video = self.video()
        self.assertEqual((video["state"], self.sent, video["file"]), ("awaiting_cuts", [], None))
        self.assertNotEqual(video["state"], "edit_failed")
        self.assertEqual(task["payload"]["edit"]["ficha"]["parts"][0]["name"], "tampa")
        self.assertEqual(self.client.get(f"/api/worker/clips/{video['id']}", headers=self.worker).json["state"], "awaiting_cuts")

    def test_item1_post_with_cuts_edits_and_sends_the_edited_clip(self):
        self.awaiting()
        task = self.stage2_task()
        edit = task["payload"]["edit"]
        self.assertEqual((edit["cuts"], edit["fidelity_note"]["score"], edit["strategy"]), ([6.4, 8.5, 10.6], 8, "uso_real_trajeto"))
        self.client.post(f"/api/worker/tasks/{task['id']}/video", data=MP4, headers={
            **self.worker, "X-Worker-Id": "vm.video", "Content-Type": "application/octet-stream"})
        self.result(task, ok=True, output=dict(duration=11.4, width=720, height=1280, fidelity_ok=True, v3=True))
        self.assertEqual((self.video()["state"], self.sent, self.video()["credits"]), ("sent", ["sendVideo"], 15))

    def test_item1_post_validation_and_wrong_state(self):
        self.assertEqual(self.client.post("/api/worker/clips/999/edit", json=dict(cuts=[1], fidelity_note=NOTE),
                                          headers=self.worker).status_code, 400)
        self.awaiting()
        vid = self.video()["id"]
        for bad in (dict(cuts=[]), dict(cuts=[8, 4]), dict(cuts="x"), dict(fidelity_note={}), dict(cuts=[-1])):
            self.assertEqual(self.cuts(vid, **bad).status_code, 400, bad)
        self.assertEqual(self.client.post(f"/api/worker/clips/{vid}/edit", json=dict(cuts=[2]), headers={}).status_code, 401)
        self.assertEqual(self.tasks(), 1)  # nenhuma tarefa de edição criada pelos pedidos inválidos
        self.stage2_task()
        self.assertEqual(self.cuts(vid).status_code, 400)  # já em edição

    def test_item1_without_cuts_for_n_hours_becomes_edit_failed_nothing_sent(self):
        self.awaiting()
        vid = self.video()["id"]
        self.assertEqual(self.store.settings()["edit_cuts_timeout_h"], 24)  # padrão, em config
        self.engine.videos.reconcile()
        self.assertEqual(self.video()["state"], "awaiting_cuts")  # ainda dentro das 24 h
        old = (datetime.now().astimezone() - timedelta(hours=25)).isoformat()
        with self.store.db() as db:
            db.execute("UPDATE videos SET updated=? WHERE id=?", (old, vid))
        self.engine.videos.reconcile()
        video = self.video()
        self.assertEqual((video["state"], self.sent), ("edit_failed", []))
        self.assertEqual(self.engine.videos.get(vid)["label"], "falha na edição, nada enviado")
        self.assertEqual(self.cuts(vid).status_code, 400)  # depois do prazo não aceita mais cortes

    def test_item1_timeout_is_a_validated_setting(self):
        self.store.save_settings(dict(edit_cuts_timeout_h=2))
        self.assertEqual(self.store.settings()["edit_cuts_timeout_h"], 2)
        with self.assertRaises(ValueError):
            self.store.save_settings(dict(edit_cuts_timeout_h=0))

    # ---- item 3: fidelidade segura o envio ----
    def _expire_stage2(self, claim):
        self.awaiting()
        vid = self.video()["id"]
        self.assertEqual(self.cuts(vid).status_code, 202)
        if claim:
            self.claim()
        with self.store.db() as db:
            db.execute("UPDATE tasks SET deadline=0 WHERE id=?", (self.video()["task_id"],))
        self.engine.videos.reconcile()
        video = self.video()
        self.assertEqual((video["state"], video["credits"], self.sent), ("edit_failed", 15, []))
        self.assertEqual(self.engine.videos.get(vid)["label"], "falha na edição, nada enviado")
        self.assertEqual(self.engine.videos.used_today(), 15)  # créditos da etapa 1 seguem contados
        self.assertIn("edit_failed", self.engine.videos.get(vid)["state"])

    def test_item1_stage2_task_expiring_queued_keeps_credits(self):
        self._expire_stage2(claim=False)

    def test_item1_stage2_task_expiring_working_keeps_credits(self):
        self._expire_stage2(claim=True)

    def test_item3_fidelity_failed_keeps_clips_and_sends_nothing(self):
        self.awaiting()
        task = self.stage2_task()
        self.result(task, ok=False, code="fidelity_failed", charged=True,
                    error="fidelity_failed: reprovado na fidelidade: nota sem a cor da parte 'tampa'")
        video = self.video()
        self.assertEqual((video["state"], self.sent), ("fidelity_rejected", []))
        self.assertEqual(self.engine.videos.get(video["id"])["label"], "reprovado na fidelidade, nada enviado")

    def test_item3_panel_refuses_an_edited_clip_without_an_approved_fidelity(self):
        self.awaiting()
        task = self.stage2_task()
        self.client.post(f"/api/worker/tasks/{task['id']}/video", data=MP4, headers={
            **self.worker, "X-Worker-Id": "vm.video", "Content-Type": "application/octet-stream"})
        self.result(task, ok=True, output=dict(duration=11.4, width=720, height=1280))  # sem fidelity_ok
        self.assertEqual((self.video()["state"], self.sent), ("fidelity_rejected", []))

    def test_item5_edit_failure_says_failure_not_uncertain(self):
        self.awaiting()
        task = self.stage2_task()
        self.result(task, ok=False, code="edit_failed", charged=True, error="edit_failed: speech_too_long")
        video = self.video()
        self.assertEqual((video["state"], self.sent), ("edit_failed", []))
        self.assertEqual(self.engine.videos.get(video["id"])["label"], "falha na edição, nada enviado")
        with self.store.db() as db:
            message = db.execute("SELECT error FROM deliveries").fetchone()[0]
        self.assertIn("Falha na edição, nada enviado", message)
        self.assertNotIn("incerto", message.lower())

    # ---- item 2: fallback sem v3 ----
    def test_item2_analysis_fallback_is_skipped_with_no_job_and_no_credit(self):
        product = dict(key="1:1", name="Pote", image="https://down-br.img.susercontent.com/file/1", price=50, rating=4.8,
                       sales=100, shop="Loja", link="https://shopee.com.br/x", commission=5)
        product["analysis"] = analysis.fallback(product, [], "modelo falhou", [], [])
        self.assertFalse(product["analysis"].get("v3"))
        run_id = self.engine.collect()
        before = self.tasks()
        video_id = self.engine.videos.enqueue(run_id, dict(product, key="9:9"))
        with self.store.db() as db:
            row = dict(db.execute("SELECT * FROM videos WHERE id=?", (video_id,)).fetchone())
        self.assertEqual((row["state"], row["credits"], self.tasks()), ("skipped", 0, before))
        self.assertIn("roteiro v3 indisponível", row["error"])
        self.assertEqual(self.engine.videos.used_today(), 0)
        with self.assertRaises(ValueError) as caught:  # manual também
            self.engine.videos.enqueue(run_id, dict(product, key="8:8"), manual=True)
        self.assertIn("roteiro v3 indisponível", str(caught.exception))
        self.assertEqual(self.tasks(), before)

    # ---- item 4: Estúdio recusado ----
    def test_item4_studio_is_refused_nothing_queued_no_subtitles(self):
        csrf = {"X-CSRF-Token": self.client.post("/api/login", json={"password": "test-password-only"}).json["csrf"]}
        with patch.object(studio_module, "STUDIO_V3_DISABLED", True), patch("subtitles.subtitle_video") as subtitle:
            self.engine.collect()
            for path, body in (("/api/studio/manual", dict(url="https://shopee.com.br/product/1/1")),
                               ("/api/studio/day", dict(run_id=1, key="1:1")),
                               ("/api/studio/1/generate", dict(cost=15))):
                response = self.client.post(path, json=body, headers=csrf)
                self.assertEqual(response.status_code, 400, path)
                self.assertEqual(response.json["error"], "Estúdio desativado no padrão v3.")
            with self.assertRaises(ValueError):
                self.engine.videos.enqueue_studio(1, "manual", None, None, "Pote", {}, {})
            self.assertEqual(self.tasks(), 0)
            subtitle.assert_not_called()
            self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
