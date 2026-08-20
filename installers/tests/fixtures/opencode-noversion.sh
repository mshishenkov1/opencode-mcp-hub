#!/usr/bin/env bash
# Фикстурный «бинарник», у которого --version не отрабатывает: код 2 без вывода (AC-103).
if [ -n "${OPENCODE_TEST_MARKER:-}" ]; then
  printf '%s\n' "$*" >>"$OPENCODE_TEST_MARKER"
fi
exit 2
