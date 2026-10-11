# Template — HARNESS.md

Substituir os colchetes. Manter as seis seções e a ordem. Máximo ~60 linhas.

```markdown
# Contrato Operacional do Projeto

> Lido pelo agente via `@HARNESS.md` no CLAUDE.md. Travado por hooks do plugin istofel-dev,
> `.claude/settings.json` e CI. Arquivo protegido: só o usuário edita.

## Entrega
- [o que esta fase/feature entrega, em 1–3 itens concretos]

## Critérios de Aceitação
- [critério verificável] — evidência: `[teste ou comando]`
- [critério verificável] — evidência: `[teste ou comando]`

## Limites Operacionais
- Escopo: modificar apenas arquivos exigidos pela tarefa atual (`.harness/scope.txt` restringe por tarefa)
- Testes existentes ([pastas de teste]) nunca são alterados nem deletados (modo `[append-only|locked]`)
- Novas dependências exigem aprovação do usuário
- Proibido: `rm -rf`, `git push --force`, `git reset --hard`, `sudo`, `--no-verify`
- Protegidos (somente usuário): .harness/**, HARNESS.md, .claude/settings.json
- [limites do domínio: invariantes, ADRs fechados, fora de escopo]

## Mapa do Projeto
- `[pasta]`: [o que contém]
- `docs/decisions.md`: log de decisões arquiteturais aceitas

## Protocolo de Verificação (executado automaticamente ao encerrar)
1. [nome]: `[comando idêntico ao --verify]`
2. [nome]: `[comando idêntico ao --verify]`

## Escalação de Falhas
- Mesmo erro [N]x seguidas **ou** [M] tentativas → parar a edição autônoma
- Imprimir o log exato, declarar a correção proposta e aguardar confirmação do usuário
```

Checklist antes do init:
- [ ] Todo critério tem evidência executável
- [ ] Toda etapa do Protocolo está entre crases e é idêntica ao `--verify`
- [ ] [N] = `--same-error` e [M] = `--max-attempts`
- [ ] Nenhuma seção com "(preencher)"
