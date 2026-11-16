#!/bin/sh
# Teste ponta a ponta das lições contextuais (hooks Stop e SessionStart).
# Uso: sh tests/test_licoes.sh   (precisa de git e python3)
set -e
S="$(cd "$(dirname "$0")/.." && pwd)/plugins/istofel-dev/scripts/harness_core.py"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT; cd "$T"
git init -q; git config user.email t@t; git config user.name t
mkdir -p src tests docs
echo "def soma(a,b): return a+b" > src/m.py
printf 'import sys; sys.path.insert(0,"src")\nfrom m import soma\nassert soma(1,2)==3\n' > tests/t.py
git add -A; git commit -qm base
python3 "$S" init --root . --tests-mode append-only --verify "test=python3 tests/t.py" --ci none >/dev/null
printf '# Lições\n\n- 2026-01-01 · [lint] 1 falha(s) · evidência: `x` · arquivos: `web/a.ts` · lição: LINT\n- 2026-01-02 · [test] 1 falha(s) · evidência: `y` · lição: TESTE\n' > docs/lessons.md
git add -A; git commit -qm harness

H() { echo "{\"cwd\":\"$T\"}" | python3 "$S" "$1"; }
ok() { if echo "$1" | grep -q "$2"; then :; else echo "FALHOU: $3"; exit 1; fi; }
no() { if echo "$1" | grep -q "$2"; then echo "FALHOU: $3"; exit 1; fi; }

out=$(H hook-session); ok "$out" LINT "sem sinal traz as recentes"; ok "$out" TESTE "sem sinal traz as recentes"

echo "def soma(a,b): return a-b" > src/m.py
out=$(H hook-stop); ok "$out" TESTE "falha traz lição do mesmo check"; no "$out" LINT "falha não traz lição de outro check"
out=$(H hook-session); ok "$out" TESTE "sessão com falha"; no "$out" LINT "sessão com falha filtra pelo check"

echo "def soma(a,b): return a+b" > src/m.py
H hook-stop >/dev/null
ok "$(tail -1 docs/lessons.md)" "arquivos: \`src/m.py\`" "stub guarda arquivo que falhou e voltou ao original"
sed -i.bak 's/lição: (pendente)/lição: NOVA/' docs/lessons.md

echo "src/**" > .harness/scope.txt
out=$(H hook-session); ok "$out" NOVA "escopo src"; no "$out" LINT "escopo src filtra"
echo "lib/**" > .harness/scope.txt
out=$(H hook-session); no "$out" "Lições" "escopo sem lição não injeta nada"
echo "ok: lições contextuais"
