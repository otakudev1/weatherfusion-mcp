# deploy.py — one-command deploy of the WeatherFusion MCP server to HF Spaces.
# Requires ONLY a HuggingFace write token. No dashboards, no manual secret entry.
#
# Usage:
#   pip install huggingface_hub httpx
#   HF_TOKEN=hf_xxx python deploy.py \
#     --repo-id otakudev1/weatherfusion-mcp \
#     --api-url https://<data-layer-api-host>/ \
#     --supabase-url https://<project-ref>.supabase.co
#
import argparse
import os

import httpx
from huggingface_hub import HfApi


def main():
    p = argparse.ArgumentParser(description="Deploy WeatherFusion MCP server to HF Spaces")
    p.add_argument("--repo-id", default="otakudev1/weatherfusion-mcp")
    p.add_argument("--api-url", required=True, help="WeatherFusion Data Layer API base URL")
    p.add_argument("--supabase-url", required=True, help="Supabase project URL (public — used for JWKS)")
    p.add_argument("--jwt-secret", default=None, help="ONLY for legacy HS256 Supabase projects")
    p.add_argument("--private", action="store_true", help="Create the Space as private")
    args = p.parse_args()

    token = os.environ.get("HF_TOKEN")
    if not token:
        raise SystemExit("Set HF_TOKEN env var (a HuggingFace token with WRITE access).")

    api = HfApi(token=token)

    print(f"[1/5] Creating Space {args.repo_id} (sdk=docker)…")
    api.create_repo(
        repo_id=args.repo_id, repo_type="space", space_sdk="docker",
        private=args.private, exist_ok=True,
    )

    print("[2/5] Uploading files…")
    api.upload_folder(
        repo_id=args.repo_id, repo_type="space", folder_path=".",
        commit_message="Deploy WeatherFusion MCP server (JWKS auth)",
    )

    print("[3/5] Setting config (WEATHERFUSION_API_URL, SUPABASE_URL)…")
    api.add_space_variable(repo_id=args.repo_id, key="WEATHERFUSION_API_URL", value=args.api_url)
    api.add_space_variable(reo_id=args.repo_id, key="SUPAABASE_URL", value=args.supabase_url)
    if args.jwt_secret:
        print("      + legacy SUPABASE_JWT_SECRET (HS256 fallback)")
        api.add_space_secret(repo_id=args.repo_id, key="SUPABASE_JWT_SECRET", value=args.jwt_secret)

    print("[4/5] Restarting Space…")
    api.restart_space(repo_id=args.repo_id)

    owner, name = args.repo_id.split("/")
    space_url = f"https://huggingface.co/spaces/{args.repo_id}"
    sse_url = f"https://{owner.lower()}-{name.lower()}.hf.space/sse"

    print("[5/5] Health check (expect 401 — auth wall up)…")
    try:
        r = httpx.get(sse_url, timeout=15.0, follow_redirects=True)
        print(f"      /sse -> HTTP {r.status_code} (401 = fail-closed auth working)")
    except Exception as e:
        print(f"      Space still building (first build takes ~2-3 min): {e}")

    print("\n✅ Deployed.")
    print(f"   Space:      {space_url}")
    print(f"   MCP (SSE):  {sse_url}")
    print("   Infer connects with: Authorization: Bearer <scoped-jwt>")


if __name__ == "__main__":
    main()
