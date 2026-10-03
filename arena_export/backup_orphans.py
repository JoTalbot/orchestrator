#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Бэкап «чатов-сирот» Arena: локальный архив + копия в S3-совместимое хранилище.

Зачем: часть чатов, которые выгружались в `data/arena/` 17–25.09.2026, из
истории аккаунта исчезла (удалены/поделились/истёк срок). Файлы экспорта —
единственная копия, поэтому их нужно и хранить локально, и держать off-host.

Что делает:
  1) считает сирот — файлы в `data/arena/chats/*.json`, которых нет в `index.json`;
  2) собирает staging-каталог `chats/`, `light/`, `MANIFEST.json`
     (по каждому файлу: размер, sha256, число сообщений) и `orphans_index.json`;
  3) упаковывает всё в `<out>/arena_orphans_<YYYYMMDD>.tar.gz` + `.sha256`;
  4) если задан `--s3-env` — заливает архив, контрольную сумму и манифест
     в S3-совместимое хранилище и сверяет скачанную копию по sha256.

Примеры:

    # только локальный архив
    sudo python3 arena_export/backup_orphans.py --out /opt/orchestrator/backups

    # с выгрузкой в OCI Object Storage (ключи берутся из env-файла)
    sudo python3 arena_export/backup_orphans.py --s3-env /etc/octopus/immortal-oci.env \
        --s3-prefix orchestrator/backups/arena

Переменные env-файла (OCI/R2-совместимые):
    IMM_S3_OCI_ENDPOINT, IMM_S3_OCI_BUCKET, IMM_S3_OCI_REGION,
    IMM_S3_OCI_KEY, IMM_S3_OCI_SECRET
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import glob
import shutil
import sys
import tarfile
import tempfile
import time

DEFAULT_DATA = "/opt/orchestrator/data/arena"
DEFAULT_OUT = "/opt/orchestrator/backups"


def sha256(path: str, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(buf):
            h.update(chunk)
    return h.hexdigest()


def load_env_file(path: str) -> dict:
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def build_archive(data_dir: str, out_dir: str, stamp: str) -> tuple[str, dict]:
    idx = json.load(open(f"{data_dir}/index.json", encoding="utf-8"))
    entries = idx if isinstance(idx, list) else idx.get("entries", [])
    live = {e["id"] for e in entries}
    chats = {os.path.basename(p)[:-5]: p for p in glob.glob(f"{data_dir}/chats/*.json")}
    orphans = sorted(set(chats) - live)

    name = f"arena_orphans_{stamp}"
    stage = os.path.join(out_dir, name)
    tar_path = os.path.join(out_dir, name + ".tar.gz")
    os.makedirs(out_dir, exist_ok=True)
    if os.path.exists(stage):
        shutil.rmtree(stage)
    for sub in ("chats", "light"):
        os.makedirs(os.path.join(stage, sub), exist_ok=True)

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "reason": "чаты были в истории Arena, затем исчезли из аккаунта; "
                  "файлы экспорта — единственная копия",
        "chats": [], "light": [], "missing_light": [],
    }
    total = 0
    for cid in orphans:
        src = chats[cid]
        shutil.copy2(src, os.path.join(stage, "chats", cid + ".json"))
        d = json.load(open(src, encoding="utf-8"))
        n = len(d.get("messages") or [])
        total += n
        manifest["chats"].append({
            "id": cid, "messages": n, "bytes": os.path.getsize(src),
            "sha256": sha256(src), "title": (d.get("title") or "")[:200],
            "updatedAt": d.get("updatedAt"),
        })
        light = f"{data_dir}/light/{cid}.json"
        if os.path.exists(light):
            shutil.copy2(light, os.path.join(stage, "light", cid + ".json"))
            manifest["light"].append({"id": cid, "bytes": os.path.getsize(light),
                                      "sha256": sha256(light)})
        else:
            manifest["missing_light"].append(cid)

    manifest["chat_count"] = len(manifest["chats"])
    manifest["light_count"] = len(manifest["light"])
    manifest["total_messages"] = total
    with open(os.path.join(stage, "MANIFEST.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    with open(os.path.join(stage, "orphans_index.json"), "w", encoding="utf-8") as f:
        json.dump([{k: c[k] for k in ("id", "title", "messages", "updatedAt")}
                   for c in manifest["chats"]], f, ensure_ascii=False, indent=1)

    with tarfile.open(tar_path, "w:gz", compresslevel=9) as t:
        t.add(stage, arcname=name)
    digest = sha256(tar_path)
    with open(tar_path + ".sha256", "w", encoding="utf-8") as f:
        f.write(f"{digest}  {os.path.basename(tar_path)}\n")
    manifest["archive"] = {"path": tar_path, "bytes": os.path.getsize(tar_path),
                           "sha256": digest}
    return tar_path, manifest


def upload_s3(tar_path: str, manifest: dict, env_file: str, prefix_root: str,
              stamp: str) -> list[str]:
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        sys.exit("нет boto3 (apt install python3-boto3)")

    env = load_env_file(env_file)
    required = ("IMM_S3_OCI_ENDPOINT", "IMM_S3_OCI_BUCKET", "IMM_S3_OCI_REGION",
                "IMM_S3_OCI_KEY", "IMM_S3_OCI_SECRET")
    missing = [k for k in required if not env.get(k)]
    if missing:
        sys.exit("в %s нет переменных: %s" % (env_file, ", ".join(missing)))

    s3 = boto3.client(
        "s3", endpoint_url=env["IMM_S3_OCI_ENDPOINT"],
        aws_access_key_id=env["IMM_S3_OCI_KEY"],
        aws_secret_access_key=env["IMM_S3_OCI_SECRET"],
        region_name=env["IMM_S3_OCI_REGION"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )
    bucket = env["IMM_S3_OCI_BUCKET"]
    prefix = f"{prefix_root}/{stamp}/"
    stage = os.path.dirname(tar_path)
    uploads = [
        (tar_path, prefix + os.path.basename(tar_path), tar_path),
        (tar_path + ".sha256", prefix + os.path.basename(tar_path) + ".sha256", tar_path),
        (os.path.join(stage, os.path.basename(tar_path)[:-7], "MANIFEST.json"),
         prefix + "MANIFEST.json", os.path.join(stage, os.path.basename(tar_path)[:-7], "MANIFEST.json")),
    ]
    keys = []
    for local, key, check_src in uploads:
        s3.upload_file(local, bucket, key)
        head = s3.head_object(Bucket=bucket, Key=key)
        print(f"  загружено: s3://{bucket}/{key} ({head['ContentLength']} байт)")
        keys.append(key)
    tmp = tempfile.mktemp()
    s3.download_file(bucket, keys[0], tmp)
    back = sha256(tmp)
    os.unlink(tmp)
    ok = back == manifest["archive"]["sha256"]
    print("  проверка копии:", "СОВПАЛА" if ok else f"РАСХОЖДЕНИЕ ({back})")
    if not ok:
        sys.exit("копия в хранилище повреждена")
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description="Бэкап чатов-сирот Arena")
    ap.add_argument("--data-dir", default=DEFAULT_DATA)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--stamp", default=time.strftime("%Y%m%d"))
    ap.add_argument("--s3-env", help="env-файл с ключами S3-совместимого хранилища")
    ap.add_argument("--s3-prefix", default="orchestrator/backups/arena")
    a = ap.parse_args()

    tar_path, manifest = build_archive(a.data_dir, a.out, a.stamp)
    print(f"архив: {tar_path} — {manifest['archive']['bytes'] / 1e6:.1f} МБ, "
          f"чатов {manifest['chat_count']}, сообщений {manifest['total_messages']}")
    print("sha256:", manifest["archive"]["sha256"])
    if a.s3_env:
        upload_s3(tar_path, manifest, a.s3_env, a.s3_prefix, a.stamp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
