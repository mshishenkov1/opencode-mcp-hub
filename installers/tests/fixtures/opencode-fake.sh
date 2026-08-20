#!/usr/bin/env bash
# Фикстурный «бинарник» OpenCode (N5-T1): печатает версию по --version и диагностику по
# `corp status`. Настоящий бинарник в тестах не нужен и не используется.
# Версия подставляется хелпером (строка @@VERSION@@).
if [ -n "${OPENCODE_TEST_MARKER:-}" ]; then
  printf '%s\n' "$*" >>"$OPENCODE_TEST_MARKER"
fi
case "${1:-}" in
  --version)
    printf '%s\n' '@@VERSION@@'
    exit 0
    ;;
  corp)
    if [ "${2:-}" = "status" ]; then
      # S-A11: без ключа команда штатно завершается кодом 1.
      printf '%s\n' 'Hub: https://hub.test'
      printf '%s\n' 'Ключ: не найден'
      exit 1
    fi
    ;;
esac
exit 0
