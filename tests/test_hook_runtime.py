"""Exercise the actual JS receiver without sending notifications or HTTP."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("failures,green,attempts", [(1, True, 2), (3, False, 3)])
def test_refresh_retry_reports_final_result_only(tmp_path, failures, green, attempts):
    source = ROOT / "litellm-free-hook"
    shutil.copy(source / "index.js", tmp_path / "index.mjs")
    shutil.copy(source / "openclaw.plugin.json", tmp_path)
    refresh = tmp_path / "refresh"
    refresh.write_text('#!/bin/sh\nn=0\n[ ! -f "$0.count" ] || n=$(cat "$0.count")\n'
                       f'n=$((n+1)); echo "$n" > "$0.count"\n[ "$n" -gt {failures} ]\n')
    refresh.chmod(0o700)
    notify = tmp_path / "notify.py"
    notify.write_text('import json,sys\nfrom pathlib import Path\n'
                      'with Path(__file__).with_suffix(".log").open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    (tmp_path / "config.json").write_text(json.dumps({
        "notifyRoute": "fixture", "notifyScript": str(notify), "refreshScript": str(refresh),
        "refreshTimeoutSeconds": 10, "notifyTimeoutSeconds": 5,
    }))
    runner = '''
import {Readable} from 'node:stream';
import plugin from './index.mjs';
process.env.OPENCLAW_GATEWAY_TOKEN='unit-test-only';
let handler;
plugin.register({registerHttpRoute:r=>handler=r.handler,logger:{info(){},warn(){},error(){}}});
const body={schema_version:1,source:'litellm-free',event:'import_succeeded',event_id:'same',
diff:{changed:true,added:[{model_name:'fixture/new'}],removed:[],updated:[]}};
async function request(){
 const req=Readable.from([Buffer.from(JSON.stringify(body))]);
 req.headers={authorization:'Bearer unit-test-only'}; req.method='POST';
 let status;
 await handler(req,{writeHead:c=>status=c,end:b=>console.log(JSON.stringify({status,body:JSON.parse(b)})),setHeader(){}});
}
await request();
'''
    if green:
        runner += "await request();\n"
    result = subprocess.run(["node", "--input-type=module", "-e", runner], cwd=tmp_path,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert responses[0]["status"] == (200 if green else 503)
    if green:
        assert responses[1]["body"]["duplicate"] is True
    messages = [json.loads(line) for line in notify.with_suffix(".log").read_text().splitlines()]
    assert len(messages) == 1
    text = messages[0][messages[0].index("--text") + 1]
    assert ("models reloaded" in text) is green
    assert ("client model reload failed" in text) is (not green)
    assert "Update not valid" not in text
    assert int(refresh.with_suffix(".count").read_text()) == attempts
    receipt = json.loads(next((tmp_path / "worker/receipts").glob("*.json")).read_text())
    assert len(receipt["refreshHistory"]) == attempts
