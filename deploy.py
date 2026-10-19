"""One-command deploy of the WeatherFusion MCP server to HF Spaces.
Requires only a HuggingFace token with WRITE access:
  pip install huggingface_hub httpx
  HF_TOKEN=hf_xxx python deploy.py --api-url https://API-HOST --supabase-url https://PROJ.supabase.co
"""
import argparse
import os

import httpx
from huggingface_hub import HfApi

p = argparse.ArgumentParser()
p.add_argument("--repo-id", default="otakudev1/weatherfusion-mcp")
p.add_argument("--api-url", required=True, help="WeatherFusion Data Layer API base URL")
p.add_argument("--supabase-url", required=True, help="Supabase project URL (public, used for JWKS)")
p.add_argument("--jwt-secret", default=None, help="Legacy HS256 projects only")
p.add_argument("--private", action="store_true")
a = p.parse_args()

tok = os.environ.get("HF_TOKEN")
if not tok:
    raise SystemExit("Set HF_TOKEN env var (HuggingFace token with WRITE access).")

api = HfApi(token=tok)
api.create_repo(repo_id=a.repo_id, repo_type="space", space_sdk="docker",
                private=a.private, exist_ok=True)
api.upload_folder(repo_id=a.repo_id, repo_type="space", folder_path=".",
                  commit_message="Deploy WeatherFusion MCP (JWKS auth)")
api.add_space_variable(repo_id=a.repo_id, key="WEATHERFUSION_API_URL", value=a.api_url)
api.add_space_variable(repo_id=a.repo_id, key="SUPABASE_URL", value=a.supabase_url)
if a.jwt_secret:
    api.add_space_secret(repo_id=a.repo_id, key="SUPABASE_JWT_SECRET", value=a.jwt_secret)
api.restart_space(repo_id=a.repo_id)

o, n = a.repo_id.split("/")
sse = "https://" + o.lower() + "-" + n.lower() + ".hf.space/sse"
print("Deployed.")
print("Space:", "https://huggingface.co/spaces/" + a.repo_id)
print("MCP (SSE):", sse)
try:
    r = httpx.get(sse, timeout=15, follow_redirects=True)
    print("Health /sse ->", r.status_code, "(401 = fail-closed auth working)")
except Exception as e:
    print("Space still building (~2-3 min):", e)
