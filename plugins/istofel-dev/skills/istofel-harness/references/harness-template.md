# Template — HARNESS.md

Carregado em toda sessão via `@HARNESS.md`. Teste de cada linha: o agente erraria sem ela?
Teto ~300 palavras. Omitir itens sem dados reais; nunca inventar.

```markdown
# Contrato Operacional

## Entrega
- [o que o passo atual entrega, em 1–3 itens concretos]

## Critérios de Aceitação
- [critério verificável] — evidência: `[teste ou comando]`

## Limites
- [fora de escopo / ADR / invariante que o agente tentaria violar] ([origem: PRD §x, ADR-y, INV-z])

## Ferramentas
- [comando ou cuidado fora do padrão, ex.: subir serviço antes dos testes]
  (ou: "- (nenhum comando fora do padrão)")

## Protocolo de Verificação
1. [nome]: `[comando idêntico ao --verify]`

## Escalação
- Mesmo erro [N]x **ou** [M] tentativas → parar, mostrar o log, propor a correção e aguardar
```

Não incluir (descobrível ou já imposto pelo hook): mapa/árvore de diretórios, stack, comandos padrão
(`npm test`, `pytest`) fora do Protocolo, lista de arquivos protegidos, comandos proibidos genéricos.

Checklist antes do init:
- [ ] Todo critério tem evidência executável
- [ ] Toda etapa do Protocolo está entre crases e é idêntica ao `--verify`
- [ ] [N] = `--same-error` e [M] = `--max-attempts`
- [ ] Nada repetido do CLAUDE.md; nenhuma árvore ou tabela de stack
- [ ] Nenhuma seção com "(preencher)"; até ~300 palavras
