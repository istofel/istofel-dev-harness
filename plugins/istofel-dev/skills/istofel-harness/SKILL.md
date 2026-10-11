---
name: istofel-harness
description: >
  Configura e audita um contrato operacional EXECUTÁVEL para agentes de codificação no projeto
  atual: HARNESS.md (entrega, critérios de aceitação, limites, mapa, verificação, escalação)
  com travas reais — hooks que bloqueiam ações proibidas e impedem encerrar sem os testes
  passarem, permissões no .claude/settings.json, proteção de integridade dos testes, sandbox
  e CI. Use SEMPRE que o usuário pedir "harness", "HARNESS.md", "contrato do agente",
  "travas", "o agente pula verificação", "o agente diz que terminou mas está quebrado",
  "proteger testes", "configurar verificação obrigatória", "auditar o harness",
  "/istofel-harness", ou ao final do istofel-project-plan (Etapa 5).
---

# istofel-harness

Regras escritas orientam; mecanismos garantem. Esta skill cria os dois e os mantém coerentes.

Script central (use sempre este caminho; o lançador encontra o Python no Linux, macOS e Windows):
`sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" <comando>`

Após o init, o usuário também tem uma cópia no projeto: `python3 .harness/harness.py <comando>`
(no Windows: `python .harness/harness.py <comando>`). Ao orientar o usuário, use a forma do SO dele.

Escolher o modo pelo pedido: **Configurar** (padrão quando não existe `.harness/config.json`),
**Auditar** (quando já existe ou o usuário pede revisão), **Operar** (dúvidas de uso).

---

## Modo Configurar

1. **Pré-checagem**
   - Se `.harness/config.json` já existir: não reconfigurar. Ir para Auditar e informar que
     reconfigurar é operação do usuário no terminal: `python3 .harness/harness.py init --force ...`
   - Se não for repositório Git: propor `git init && git add -A && git commit -m "base"` e
     aguardar o "ok" — o Git é a rede de segurança para desfazer erros.

2. **Coletar insumos (sem perguntar o que já se sabe)**
   - Ler `package.json`, `pyproject.toml`/`requirements*.txt`, estrutura de pastas e, se
     existirem, `docs/spec.md`, `docs/prd.md`, `CLAUDE.md`.
   - Rodar `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" audit --root .` para ver o
     estado atual (é esperado falhar antes do init).
   - Derivar: Entrega da fase atual, Critérios de Aceitação, comandos de verificação
     (lint/typecheck/testes), pastas de teste e mapa do projeto. Vindo do istofel-project-plan,
     usar a tabela da Etapa 5 dele.

3. **Perguntar no máximo 3 coisas** (AskUserQuestion), só o que faltar:
   - Modo dos testes: `append-only` (recomendado: pode criar testes novos, nunca alterar os
     existentes) · `locked` (agente não toca em testes) · `open` (sem proteção)
   - Sandbox do SO (recomendado em macOS/Linux; no Linux exige `bubblewrap`; Windows só via WSL2)
   - CI no GitHub Actions (recomendado se o repositório estiver no GitHub)
   - Se nenhum comando de verificação foi detectado, perguntar qual comando roda os testes.

4. **Escrever o HARNESS.md ANTES do init** (o init o protege em seguida). Estrutura obrigatória:
   `Entrega` · `Critérios de Aceitação` · `Limites Operacionais` · `Mapa do Projeto` ·
   `Protocolo de Verificação` · `Escalação de Falhas`. Regras de qualidade:
   - Cada critério de aceitação é verificável e aponta o teste/comando que o comprova.
   - Cada etapa do Protocolo é um comando entre crases, idêntico ao `--verify`. Nada de
     "verifique se está correto": se não há comando, criar o script (ex.: `validate:schema`)
     ou registrar como pendência para o usuário.
   - Os números da escalação são exatamente `--same-error` e `--max-attempts`.
   - Mapa curto: só onde ficam código, testes, docs, schemas e decisões.
   - Máximo ~60 linhas. Use o template em `references/harness-template.md`.

5. **Rodar o init** (exemplo; ajuste aos dados reais):
   ```bash
   sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" init --root . \
     --tests-mode append-only --max-attempts 4 --same-error 2 \
     --verify "lint=npm run lint" --verify "test=npm test" \
     --ci github --sandbox
   ```
   Sem `--sandbox` se o usuário recusou; `--ci none` se não usar GitHub. O init cria
   `.harness/` (config, baseline dos testes, cópia do CLI), mescla `.claude/settings.json`,
   adiciona `@HARNESS.md` ao `CLAUDE.md` e gera o workflow de CI. Não sobrescreve o HARNESS.md.

6. **Validar**
   - `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" verify` — a base precisa estar verde. Se falhar: mostrar o
     log e perguntar ao usuário. NUNCA alterar testes para fazer passar.
   - `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" audit --root .` — mostrar o relatório.

7. **Entregar** ao usuário, em linguagem simples: o que ficou travado, como usar (seção
   Operar) e sugerir o commit:
   `git add HARNESS.md CLAUDE.md .harness .claude/settings.json .github && git commit -m "harness"`.
   Avisar: reiniciar a sessão do Claude Code para carregar permissões e sandbox.

---

## Modo Auditar

1. Rodar `sh "${CLAUDE_PLUGIN_ROOT}/scripts/run.sh" audit --root .`.
2. Revisão qualitativa do HARNESS.md, além do script:
   - critérios vagos ou sem teste correspondente;
   - contradições entre texto e `.harness/config.json` (números, comandos, caminhos);
   - etapas de verificação que dependem do julgamento do modelo;
   - limites prometidos sem trava correspondente (hook, permissão, sandbox ou CI).
3. Entregar: tabela curta `Achado | Risco | Correção`. Arquivos protegidos (`HARNESS.md`,
   `.harness/**`, `.claude/settings.json`) não são editáveis pelo agente: propor o texto exato
   para o usuário aplicar, ou instruir `HARNESS_DISABLE=1 claude` para uma sessão de manutenção.

---

## Modo Operar (respostas rápidas)

- **Como funciona a trava de encerramento:** ao terminar, o hook roda os comandos do contrato;
  falhou → o agente é obrigado a continuar; mesmo erro 2x ou 4 tentativas → escalação (edição
  bloqueada até o usuário responder).
- **Restringir uma tarefa a arquivos específicos (usuário, no terminal):**
  `python3 .harness/harness.py scope "src/auth/**" "tests/auth/**"` · liberar: `scope --clear`
- **Aprovar testes novos após revisá-los (usuário):** `python3 .harness/harness.py approve-tests`
- **Ver estado:** `python3 .harness/harness.py status`
- **Destravar após escalação:** basta o usuário responder no chat; ou `harness.py reset`.
- **Manutenção do contrato:** iniciar com `HARNESS_DISABLE=1 claude` (desliga os hooks só nessa sessão).
- Comandos `init`, `approve-tests`, `scope` e `reset` são exclusivos do usuário: o guard os
  bloqueia quando o agente tenta. Não tentar contornar.

Detalhes das travas e limites conhecidos: `references/travas.md`.
