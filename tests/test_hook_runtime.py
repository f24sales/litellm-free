
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location("handler",Path(__file__).resolve().parents[1]/"mcp/litellm-free/litellm-free.py")
handler=importlib.util.module_from_spec(spec)
spec.loader.exec_module(handler)

def event(kind="import_succeeded"):
    return {"schema_version":1,"source":"litellm-free","event":kind,"event_id":"test-1",
            "diff":{"changed":True,"added":[{"model_name":"model-a","api_key":"secret"}],"removed":[],"updated":[]}}

def prepare(tmp_path,payload,runner=lambda *_:{"ok":True}):
    return handler.handle({"action":"prepare","payload":payload,"state_dir":str(tmp_path)},runner)

def receipt(tmp_path,result,status="sent"):
    return handler.handle({"action":"receipt","receipt_id":result["receipt_id"],"delivery":{"status":status},"state_dir":str(tmp_path)})

def test_refresh_diff_attachment_and_idempotent_delivery(tmp_path):
    calls=[]
    def refresh(*args):
        calls.append(1)
        return {"ok":True}
    result=prepare(tmp_path,event(),refresh)
    assert result["ok"] and len(calls)==1
    assert "model-a" in result["text"]
    assert "secret" not in Path(result["attachments"][0]).read_text()
    receipt(tmp_path,result)
    assert prepare(tmp_path,event(),refresh)["duplicate"]
    assert len(calls)==1

def test_delivery_failure_does_not_repeat_successful_refresh(tmp_path):
    calls=[]
    def refresh(*args):
        calls.append(1)
        return {"ok":True}
    result=prepare(tmp_path,event(),refresh)
    receipt(tmp_path,result,"failed")
    retry=prepare(tmp_path,event(),refresh)
    assert retry["ok"] and len(calls)==1
    assert not retry.get("duplicate")

def test_uncertain_delivery_is_not_resent(tmp_path):
    result=prepare(tmp_path,event())
    assert prepare(tmp_path,event())["duplicate"]
    receipt(tmp_path,result,"delivery_unknown")
    assert prepare(tmp_path,event())["duplicate"]

def test_event_id_cannot_be_reused_for_changed_payload(tmp_path):
    prepare(tmp_path,event())
    changed=event();changed["diff"]["added"][0]["model_name"]="other"
    with pytest.raises(ValueError,match="event_id_reused"):
        prepare(tmp_path,changed)

def test_refresh_failure_is_reported_separately(tmp_path,monkeypatch):
    monkeypatch.setattr(handler.time,"sleep",lambda _:None)
    result=prepare(tmp_path,event(),lambda *_:{"ok":False,"exitCode":1})
    assert result["ok"] is True
    assert result["refresh"]["ok"] is False
    assert "reload failed" in result["text"]
    assert len(result["attachments"]) == 1

def test_diff_attachment_keeps_all_model_rows(tmp_path):
    payload = event()
    payload["diff"]["added"] = [{"model_name": f"model-{i}", "provider": "test"} for i in range(20)]
    result = prepare(tmp_path, payload)
    attachment = Path(result["attachments"][0])
    saved = json.loads(attachment.read_text())
    assert len(saved["added"]) == 20
    assert [row["model_name"] for row in saved["added"]] == [f"model-{i}" for i in range(20)]

def test_scan_pdf_is_verified_and_stored_beneath_handler_state(tmp_path):
    data=b"%PDF-1.4\\nfixture"
    payload={"schema_version":1,"source":"litellm-free-webui","event":"scan_valid","scan_state":"ok_changed",
             "report":{"filename":"scan-report-20261102T120000Z.pdf","content_type":"application/pdf",
                       "content_base64":base64.b64encode(data).decode(),"sha256":hashlib.sha256(data).hexdigest()}}
    result=prepare(tmp_path,payload)
    assert Path(result["attachments"][0]).read_bytes()==data
    payload["report"]["sha256"]="0"*64
    with pytest.raises(ValueError,match="invalid_event"):
        prepare(tmp_path,payload)

def test_invalid_events_rejected(tmp_path):
    with pytest.raises(ValueError,match="invalid_event"):
        prepare(tmp_path,{"event":"execute","command":"anything"})
