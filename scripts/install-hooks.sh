#!/usr/bin/env bash
# Git hooks 설치 — .githooks/ 디렉토리를 hooksPath로 설정 + 실행권한 부여.
# 한 번만 실행하면 됨. 다른 머신에서 clone 후에도 한 번 실행 필요.
set -e
REPO=$(git rev-parse --show-toplevel)
chmod +x "$REPO"/.githooks/*
git config core.hooksPath .githooks
echo "OK — core.hooksPath = .githooks (hooks: $(ls "$REPO"/.githooks))"
