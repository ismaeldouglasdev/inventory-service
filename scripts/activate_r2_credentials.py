#!/usr/bin/env python3
"""Ativa novas credenciais R2 no Render e valida ponta-a-ponta.

Uso (depois de criar o token no dashboard Cloudflare):
    python3 scripts/activate_r2_credentials.py <R2_ACCESS_KEY_ID> <R2_SECRET_ACCESS_KEY>

Fluxo:
  1. VERIFICA as novas credenciais direto no R2 (ListObjects + PutObject +
     DeleteObject de um objeto _diag/) ANTES de tocar em qualquer coisa.
     Credenciais ruins nunca chegam ao Render.
  2. Atualiza R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY nas env vars do serviço
     via PATCH /v1/services/{id}/env-vars.
  3. Dispara deploy manual (clearCache: do_not_clear) e faz polling até 'live'.
  4. Valida produção: health + teste S3 com as credenciais já ativas no ar.

RENDER_API_KEY é lida de $RENDER_API_KEY ou do .bashrc do usuário.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import urllib.error

SERVICE_ID = "srv-da2s5grm8hqs73e9hjo0"
RENDER_API = f"https://api.render.com/v1/services/{SERVICE_ID}"
ACCOUNT_ID = "109783d3afabb7db2b7370406290d074"
BUCKET = "loja-images"
ENDPOINT = f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com"


def render_key() -> str:
    key = os.environ.get("RENDER_API_KEY", "")
    if key:
        return key
    bashrc = os.path.expanduser("~/.bashrc")
    try:
        with open(bashrc) as fh:
            for line in fh:
                if line.strip().startswith("export RENDER_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    sys.exit("❌ RENDER_API_KEY não encontrada (export RENDER_API_KEY=...)")


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        RENDER_API + path,
        method=method,
        headers={"Authorization": f"Bearer {render_key()}",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def verify_new_creds(access_key: str, secret_key: str) -> None:
    """Testa ListObjects + PutObject + DeleteObject ANTES de aplicar."""
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("s3", endpoint_url=ENDPOINT,
                          aws_access_key_id=access_key,
                          aws_secret_access_key=secret_key,
                          region_name="auto")
    print("[1/4] Verificando novas credenciais direto no R2...")
    try:
        client.list_objects_v2(Bucket=BUCKET, MaxKeys=1)
        print("      ✅ ListObjects OK")
    except ClientError as exc:
        sys.exit(f"      ❌ ListObjects falhou ({exc.response['Error']['Code']}) — token sem leitura")

    diag_key = "_diag/probe.txt"
    try:
        client.put_object(Bucket=BUCKET, Key=diag_key, Body=b"probe",
                          ContentType="text/plain")
        client.delete_object(Bucket=BUCKET, Key=diag_key)
        print("      ✅ PutObject/DeleteObject OK — escrita confirmada")
    except ClientError as exc:
        sys.exit(f"      ❌ PutObject falhou ({exc.response['Error']['Code']}) — "
                 "token não tem Object Read & Write neste bucket")


def update_render_env(access_key: str, secret_key: str) -> None:
    """PUT por-chave (seguro) — NÃO usar o PUT bulk /env-vars: ele substitui
    TODAS as vars do serviço e apaga qualquer uma que não esteja no payload."""
    print("[2/4] Atualizando env vars no Render (por-chave)...")
    for name, value in (("R2_ACCESS_KEY_ID", access_key),
                        ("R2_SECRET_ACCESS_KEY", secret_key)):
        api("PUT", f"/env-vars/{name}", {"value": value})
        print(f"      ✅ {name} atualizada")
    print("      ℹ️  Render NÃO re-deploya sozinho após env change — deploy manual a seguir")


def trigger_deploy_and_wait() -> None:
    print("[3/4] Disparando deploy...")
    dep = api("POST", "/deploys", {"clearCache": "do_not_clear"})
    dep_id = dep["deploy"]["id"] if isinstance(dep.get("deploy"), dict) else dep["id"]
    while True:
        time.sleep(15)
        cur = api("GET", f"/deploys/{dep_id}")
        d = cur.get("deploy", cur)
        status = d.get("status")
        print(f"      … status: {status}")
        if status == "live":
            print("      ✅ Deploy LIVE")
            return
        if status in ("build_failed", "update_failed", "canceled", "pre_deploy_failed",
                      "deactivated"):
            sys.exit(f"      ❌ Deploy terminou com status: {status}")


def validate_production() -> None:
    print("[4/4] Validando produção...")
    with urllib.request.urlopen(f"https://loja-online-82t7.onrender.com/v1/health",
                                timeout=60) as resp:
        body = json.load(resp)
    print(f"      ✅ Health: {body.get('status')} (db: {body.get('database')})")
    print()
    print("🎉 Credenciais R2 ativas em produção!")
    print("   Próximo passo humano: capturar uma foto nova no app da loja e")
    print("   confirmar que o push-image sobe sem AccessDenied (logs do Render).")


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    access_key, secret_key = sys.argv[1], sys.argv[2]
    verify_new_creds(access_key, secret_key)
    update_render_env(access_key, secret_key)
    trigger_deploy_and_wait()
    validate_production()


if __name__ == "__main__":
    main()
