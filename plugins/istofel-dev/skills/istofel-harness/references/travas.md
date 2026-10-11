# Travas do istofel-harness — o que cada camada garante

| Regra do contrato | Trava | Onde |
|---|---|---|
| Não encerrar com verificação falhando | Hook `Stop` roda os comandos; falha → agente continua | plugin `hooks/hooks.json` |
| Escalar após falhas repetidas | Estado em `.harness/state/`; edição bloqueada até o usuário responder | hooks `Stop` + `PreToolUse` + `UserPromptSubmit` |
| Testes existentes intocáveis | Guard nega Edit/Write em arquivo da baseline + checagem de hash a cada encerramento e no CI | `hook-guard`, `baseline.json`, CI |
| Arquivos do harness protegidos | Guard + `permissions.deny Edit(...)` + sandbox `denyWrite` | hook + `.claude/settings.json` |
| Sem comandos destrutivos | Guard (rm -rf, push --force, reset --hard, clean -f, sudo, --no-verify, curl\|sh) + deny no settings | hook + settings |
| Dependências só com aprovação | Guard retorna `ask` para instalar pacotes ou editar manifestos | hook |
| Escopo por tarefa | `.harness/scope.txt` (definido pelo usuário) limita Edit/Write | hook |
| Verificação fora da máquina do agente | GitHub Actions roda `harness.py verify --ci` | `.github/workflows/harness.yml` |
| Contrato lido em toda sessão | `@HARNESS.md` no CLAUDE.md + resumo injetado no `SessionStart` | init + hook |

## Limites conhecidos (por isso existem várias camadas)

- O guard de Bash é heurístico: um script indireto (`cd tests && ...`, programa compilado)
  pode escapar dele. A checagem de hash no encerramento e no CI pega a alteração mesmo assim,
  e o sandbox (`--sandbox`) bloqueia no nível do SO.
- Testes fracos passam. A evidência de qualidade vem dos critérios de aceitação + revisão
  humana dos testes novos antes do `approve-tests` + subagente `harness-reviewer`.
- Hooks e permissões valem no Claude Code. No chat do claude.ai, o HARNESS.md é só texto.
- O sandbox exige macOS, Linux (com `bubblewrap`) ou WSL2.
- Com sandbox + modo append-only, comandos via Bash não escrevem em pastas de teste
  (ex.: snapshots novos do Vitest). Testes novos devem ser criados com a ferramenta Write.
- `HARNESS_DISABLE=1` desliga os hooks na sessão: uso exclusivo do usuário para manutenção.
