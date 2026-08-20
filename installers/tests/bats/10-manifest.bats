#!/usr/bin/env bats
#
# Манифест: состав, валидация, безопасные пути (N5-P3, N5-P4).

setup() {
  load helpers
  setup_sandbox
  make_pkg
}

@test "AC-04: manifest.example.json валиден по manifest.schema.json и содержит обязательные поля" {
  if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
    skip "нет .venv/bin/python с jsonschema"
  fi
  run "$REPO_ROOT/.venv/bin/python" - "$INSTALLERS_ROOT/common/manifest.schema.json" \
    "$INSTALLERS_ROOT/common/manifest.example.json" <<'PY'
import json
import sys

import jsonschema

schema = json.load(open(sys.argv[1], encoding="utf-8"))
example = json.load(open(sys.argv[2], encoding="utf-8"))
jsonschema.validate(example, schema)

assert example["schema"] == 1, example["schema"]
assert example["product"] == "opencode-magnit", example["product"]
for field in ("version", "os", "arch", "hub_url", "built_at", "source_release"):
    assert example.get(field), field
for field in ("file", "sha256", "install_name"):
    assert example["ca"].get(field), field
assert isinstance(example["artifacts"], list) and example["artifacts"]
assert isinstance(example["purge_paths"], list) and example["purge_paths"]
print("ok")
PY
  assert_status 0
  assert_output_contains "ok"
}

@test "AC-05: два артефакта kind=cli → отказ с полем artifacts и кодом 2" {
  manifest_edit 's|"kind": "cli",|"kind": "cli", "file": "bin/opencode", "sha256": "0000000000000000000000000000000000000000000000000000000000000000", "install_name": "opencode" }, { "kind": "cli",|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "artifacts"
  assert_output_contains 'требуется ровно один артефакт kind="cli", найдено 2'
}

@test "AC-05: манифест без артефакта kind=cli → отказ с полем artifacts и кодом 2" {
  manifest_edit 's|"kind": "cli"|"kind": "tool"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "artifacts"
  assert_output_contains 'найдено 0'
}

@test "AC-05: два артефакта kind=desktop → отказ с полем artifacts и кодом 2" {
  PKG_DESKTOP=1 make_pkg
  manifest_edit 's|"kind": "desktop",|"kind": "desktop", "file": "desktop/OpenCode.dmg", "sha256": "0000000000000000000000000000000000000000000000000000000000000000" }, { "kind": "desktop",|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "artifacts"
  assert_output_contains 'не более одного артефакта kind="desktop", найдено 2'
}

@test "AC-06: манифест отсутствует → путь в сообщении, код 2, домашний каталог не изменён" {
  snapshot_home "$BATS_TEST_TMPDIR/before"
  rm -f "$PKG/common/manifest.json"
  oc_run --no-launch
  assert_status 2
  assert_output_contains "Манифест не найден: $PKG/common/manifest.json"
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-07: манифест не является JSON → сообщение о разборе, код 2, ничего не установлено" {
  printf 'это не json\n' >"$PKG/common/manifest.json"
  oc_run --no-launch
  assert_status 2
  assert_output_contains "Манифест не разбирается как JSON: $PKG/common/manifest.json"
  [ ! -e "$(bin_dir_path)/opencode" ]
  [ ! -e "$(config_dir_path)" ]
}

@test "AC-08: schema=2 → сообщение о поле schema и ожидаемом значении 1, код 2" {
  manifest_edit 's|"schema": 1|"schema": 2|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле schema"
  assert_output_contains "ожидается значение 1"
}

@test "AC-09: поле version отсутствует → сообщение о поле version, код 2" {
  manifest_edit '/"version":/d'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле version"
  assert_output_contains "обязательное поле отсутствует или пусто"
}

@test "AC-09: поле version пустое → сообщение о поле version, код 2" {
  manifest_edit 's|"version": "[^"]*"|"version": ""|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "поле version"
}

@test "AC-10: sha256 CLI-артефакта из 63 символов → требование 64 символа, код 2" {
  manifest_edit 's|^      "sha256": "\(.\{63\}\)."|      "sha256": "\1"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "sha256"
  assert_output_contains "требуется 64 символа [0-9a-f]"
}

@test "AC-10: sha256 содержит Z → требование 64 символа [0-9a-f], код 2" {
  manifest_edit 's|^      "sha256": ".|      "sha256": "Z|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "sha256"
  assert_output_contains "требуется 64 символа [0-9a-f]"
}

@test "AC-11: file=../../etc/passwd → отказ с кодом 2, файл вне пакета не читается" {
  manifest_edit 's|"file": "bin/opencode"|"file": "../../etc/passwd"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь: ../../etc/passwd"
  refute_output_contains "root:"
}

@test "AC-11: file=/etc/passwd → отказ с кодом 2" {
  manifest_edit 's|"file": "bin/opencode"|"file": "/etc/passwd"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь: /etc/passwd"
  refute_output_contains "root:"
}

@test "AC-11: file с буквой диска и обратным слэшем → отказ с кодом 2" {
  manifest_edit 's|"file": "bin/opencode"|"file": "C:\\\\x"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "недопустимый путь"
}

@test "AC-11: ca.file с ведущим слэшем → отказ с кодом 2" {
  manifest_edit 's|"file": "certs/tander-ca-bundle.pem"|"file": "/tmp/ca.pem"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "ca.file"
}

@test "AC-11: file указывает на отсутствующий в пакете файл → код 2" {
  manifest_edit 's|"file": "bin/opencode"|"file": "bin/absent"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "файл отсутствует в пакете: bin/absent"
}

@test "AC-12: невалидный манифест не меняет домашний каталог побайтово" {
  mkdir -p "$HOME/.config/opencode"
  printf '{"user":true}\n' >"$HOME/.config/opencode/opencode.json"
  printf 'export FOO=1\n' >"$HOME/.zshrc"
  snapshot_home "$BATS_TEST_TMPDIR/before"
  manifest_edit 's|"schema": 1|"schema": 7|'
  oc_run --no-launch
  assert_status 2
  snapshot_home "$BATS_TEST_TMPDIR/after"
  run diff "$BATS_TEST_TMPDIR/before" "$BATS_TEST_TMPDIR/after"
  assert_status 0
}

@test "AC-12: манифест без purge_paths → код 2 (обязательное поле)" {
  manifest_edit 's|"purge_paths"|"purge_paths_renamed"|'
  oc_run --no-launch
  assert_status 2
  assert_output_contains "purge_paths"
}
