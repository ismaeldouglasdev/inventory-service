# Regras para evitar travamento do sistema

## Proibições absolutas
- NUNCA use `glob` com `.` (diretório raiz/atual) ou padrões muito amplos como `**/*`
- NUNCA use `**/` como **prefixo** em padrões `glob` — `**/backup*`, `**/*cron*`, etc. são tão perigosos quanto `**/*`
- NUNCA faça `grep` ou `codesearch` sem especificar um diretório ou padrão de arquivo restrito
- NUNCA use `read` em diretórios muito grandes (ex: `/`, `/usr`, `/home` sem subdiretório específico)
- NUNCA faça varreduras recursivas em toda a árvore de diretórios

## Pré-check: antes de QUALQUER busca de arquivo
Pergunte-se, NESTA ORDEM:
1. **Comando direto resolve?** `crontab -l`, `systemctl status`, `ps aux`, `ls` em diretório conhecido, `journalctl` — SEMPRE tente primeiro
2. **Caminho exato do arquivo é conhecido?** Use `read` com caminho completo — zero busca necessária
3. **Só não sei o nome exato dentro de um diretório pequeno?** Use `glob` com path específico e padrão raso (ex: `pattern: "backup*"` com `path: "/home/ismael"`, sem `**/`)
4. **Precisa procurar em múltiplos lugares?** Use `task explore` em vez de `glob`/`grep`

## Regras para operações seguras
- Sempre restrinja buscas a diretórios específicos e rasos (ex: `src/`, `docs/`)
- Use `ls` antes de qualquer operação para verificar o tamanho do diretório
- Para arquivos conhecidos, use o caminho completo e direto — SEM busca
- Prefira `task` com `explore` para buscas moderadas em vez de `glob`/`grep` diretamente
- Limite `read` a arquivos individuais ou diretórios com poucos arquivos

## Operações pesadas → SEMPRE parceladas e com aviso
O PC do usuário tem **3.7Gi de RAM** e trava com CPU/IO alto. Qualquer operação pesada deve ser:
1. **Avisada antes**: dizer ao usuário que vai rodar algo pesado e perguntar se pode
2. **Parcelada**: dividir em lotes pequenos (ex.: sync de 8k produtos em chunks, processamento de poucas fotos por vez) em vez de tudo de uma vez
3. **Leve por padrão**: preferir `sync` incremental/delta a full; evitar `remove_bg` em fotos grandes; não rodar sync + rembg + commits grandes ao mesmo tempo
4. **Pausada se o usuário reclamar de travamento**: parar na hora e esperar

### Especificamente proibido durante uso ativo (sem ok do usuário)
- `POST /v1/store/sync?mode=full` (varre 8.492 produtos + dedupe) — só delta/incremental
- Processamento de imagem `remove_bg=true` em fotos de vários MB de uma vez
- Copiar/rotacionar lotes grandes de imagens de uma vez
- `npm run build`, `composer install`, `git gc`, etc. — perguntar antes
- Qualquer comando que gaste >1Gi RAM ou deixe load >4 por mais de alguns segundos

### Operações leves que podem rodar sem pedir
- `systemctl restart/is-active inventory.service` (reinício é leve)
- UPDATE/INSERT pontual no MySQL de uma linha
- `chgrp`/`chmod` pontual em arquivos conhecidos
- Deploy com `cp` arquivo-a-arquivo (vários `cp` pequenos, sem rsync gigante)
- `git add/commit` de poucos arquivos
- Rotacionar UMA imagem por vez

## Lembretes
- O sistema do usuário é limitado e trava com operações pesadas de I/O
- Priorize eficiência: operações específicas > operações abrangentes
- Em caso de dúvida, pergunte ao usuário antes de fazer uma busca potencialmente pesada
- RELEIA este arquivo antes de iniciar qualquer tarefa nova

## Otimizações aplicadas (11/ago/2026) — NÃO reverter
Máquina: i3-3220T 2C/4T, **3.7Gi RAM**, **HDD 465GB** (sda). Gargalo = HDD + RAM baixa.

### Tuning de memória/swap (`/etc/sysctl.d/99-performance.conf` + `/etc/sysctl.conf`)
- `vm.swappiness=100` + zram 1,5G (zstd) com prioridade 100 > swapfile HDD (-1) → swap vai para zram (RAM comprimida), NÃO para o HDD
- `vm.page-cluster=0` (zram lê página a página)
- `vm.dirty_ratio=6`, `vm.dirty_background_ratio=3`, `vm.dirty_expire_centisecs=1500` → escritas menores/frequentes, HDD não "congela" em picos
- `vm.vfs_cache_pressure=60` → cache de diretório/inode dura mais (menos leitura de HDD)
- NÃO alterar swappiness de volta para 10/60 — o tuning antigo (99-performance.conf) fazia swappiness=10, que enchia a RAM e batia no swapfile HDD = travamento

### I/O scheduler
- **BFQ** ativo no sda (`/etc/modules-load.d/bfq.conf` + regra `/etc/udev/rules.d/60-sched-bfq.rules`) → interface responde mesmo com I/O de fundo

### Serviços desativados (`systemctl disable --now`)
- `snapd`/`snapd.socket`/`snapd.seeded` (não há snaps de usuário) + autostarts `snap-userd`/`snap-installation-monitor` em `~/.config/autostart/` (Hidden=true)
- `blueman-applet` (**sem hardware Bluetooth**) e `lubuntu-update` (notificador de update) — overrides em `~/.config/autostart/`
- `apt-daily.timer`/`apt-daily-upgrade.timer` (evita pico de I/O surpresa — atualizar via `apt` manualmente)
- `cups-browsed` (manter `cups` — impressora Epson TM20 USB funciona normal)
- `lxc-net`/`lxcfs`/`lxc-monitord`, `kerneloops`

### Notas
- Falkon/QtWebEngine consome ~560MB (vários processos) — orientar usuário a manter 1 aba só
- Peso típico com tudo aberto: opencode ~775MB + opencode-web ~306MB + Falkon ~560MB + MariaDB ~137MB

---
# DOCUMENTAÇÃO DO SISTEMA — Jul/2026

## Stack
- **OSPOS** v3.4.1 — fork em `/home/ismael/opensourcepos-fork/` branch `merge-staging`
- **PHP** 8.3.6 + **MariaDB** 10.11.14
- **Apache** 2.4.58
- **Loja-online** (React 19 + Vite 8 + Tailwind 4) em `/home/ismael/loja-online/`
- **Inventory-service** (FastAPI) em `/home/ismael/inventory-service/`

## URLs
| Ambiente | URL | Banco | Path |
|----------|-----|-------|------|
| Produção  | http://localhost/ | `ospos` | `/var/www/html/pos/` |
| Teste     | http://localhost:8080/ | `ospos_test` | `/var/www/html/pos-test/` |
| Loja      | http://localhost:5173/ | — | `/home/ismael/loja-online/` |
| Inventory | http://localhost:8000/ | SQLite | `/home/ismael/inventory-service/` |

## Credenciais
- **OSPOS admin**: admin / Arroz123@
- **Sudo**: senha `1` (definida em SESSION_STATE.md)
- **MySQL admin**: admin / Arroz123@

## Deploy
### Fork → Teste
```bash
# Aplica alterações do fork no ambiente de teste
sudo rsync -av --delete \
  --exclude='.env' \
  --exclude='.git' \
  /home/ismael/opensourcepos-fork/ /var/www/html/pos-test/
sudo chown -R www-data:www-data /var/www/html/pos-test/writable
sudo rm -rf /var/www/html/pos-test/writable/cache/*
sudo systemctl reload apache2
```

### Fork → Produção
Script em `/home/ismael/deploy-prod.sh`. Exclui `.env` e `.git`.

**Importante:** a config do banco lê do `.env` (CI4 lê automaticamente via BaseConfig). Production `.env` tem `database.default.database = ospos`.

## Migrações BD
Migrações manuais necessárias (não rodam via `spark migrate`):

```sql
-- Coluna last_modified em ospos_items (para item salvar)
ALTER TABLE ospos_items ADD COLUMN IF NOT EXISTS last_modified DATETIME DEFAULT NULL AFTER pic_filename;

-- Coluna stock_status em ospos_item_quantities (para Home Dashboard)
ALTER TABLE ospos_item_quantities ADD COLUMN stock_status TINYINT NOT NULL DEFAULT 0 AFTER quantity;

-- Tax category nullable
ALTER TABLE ospos_items MODIFY COLUMN tax_category_id INT NULL;
```

## Fixes Realizados (Jul/2026)

### 1. Coluna `last_modified` ausente (itens não salvavam)
- **Arquivo:** `app/Models/Item.php:429` — `save_value()` envia `last_modified` no UPDATE/INSERT
- **Causa:** coluna não existia no schema original (`initial_schema.sql`)
- **Solução:** rodar migration `3.4.2_last_modified_item.sql` no banco

### 2. `Database.php` hardcoded (produção apontava para `ospos_test`)
- **Arquivo:** `app/Config/Database.php`
- **Solução:** CI4 já lê do `.env` automaticamente via BaseConfig. Production `.env` corrigido com `database.default.database = ospos`

### 3. `stock_status` column missing
- **Arquivo:** `app/Controllers/Home.php:92-97` — Dashboard usa `stock_status`
- **Causa:** coluna nunca adicionada ao schema
- **Solução:** `ALTER TABLE` manual adicionando `stock_status TINYINT DEFAULT 0`

### 4. Modal checkout — FINALIZAR empurrado pra fora
- **Arquivo:** `app/Views/sales/register.php`
- **Solução:** botão movido do `.modal-body` para `.modal-footer` (sempre visível). Body com `overflow-y: auto` e `flex: 1 1 auto` dentro de content com `max-height: 85vh`. Modal largado para 520px.

### 5. Auto-finish removido para Dinheiro e PIX
- **Arquivo:** `app/Views/sales/register.php:1321-1330` — `addPayment()`
- **Comportamento:**
  - Dinheiro / PIX → mostra troco, foco no FINALIZAR (confirmação manual)
  - Débito / Crédito / Fiado → finaliza automaticamente

### 6. Debugbar quebrando JSON do addDiversos
- **Arquivo:** `app/Controllers/Sales.php:1680-1682`
- **Causa:** `echo json_encode(...)` sem `Content-Type: application/json` fazia CI4 anexar debugbar no body
- **Solução:** `$this->response->setContentType('application/json')` antes do `echo`

### 7. CSS duplicado e conflitante no register.php
- **Arquivo:** `app/Views/sales/register.php` — 5 blocos `<style>` com regras conflitantes
- **Solução:** consolidado em 1 bloco, removidas regras obsoletas

### 8. Tabela de items enorme
- **Arquivo:** `public/css/modern.css`
- **Solução:** adicionado CSS compacto para `.bootstrap-table .table` (fonte 0.8rem, padding 4px 6px, scroll horizontal, etc.)

### 9. `Whoops!` no `/sales/add` — ValueError do bcadd
- **Arquivo:** `app/Libraries/Sale_lib.php`
- **Sintoma:** 500 "Whoops!" em `/sales/add`, `/sales/quickFinish`; log: `ValueError: bcadd(): Argument #1 ($num1) is not well-formed`
- **Causa:** `parse_decimals()` retorna `false`/valores inválidos (notação científica ex: `1.1111111111111E+32` digitado no checkout) que eram salvos como `payment_amount` na sessão (`ospos_sessions`). O `is_numeric()` aceita exponenciais, mas `bcadd()` do PHP 8.3 rejeita e lança ValueError, travando TODA página de venda.
- **Solução:** método privado `to_bcmath_number()` (normaliza qualquer valor para decimal bcmath válido) usado em `add_payment()` e `get_payments_total()`. Aplicado em prod, fork e teste.
- **Aplicado em:** 31/jul/2026. Commitado em `fdfa8fea9`.

### 10. Botão FINALIZAR/Enviar "não funcionava" após um clique (dead submit)
- **Arquivos:** `public/js/manage_tables.js` (fonte) + bundle `public/resources/opensourcepos-39c74204a5.min.js` (prod, teste e fork)
- **Sintoma:** ao clicar em Enviar no modal de edição de item, o botão parava de responder (sem erro no log CI4 — era client-side, passou batido pelo guardrail antigo)
- **Causa:** em `dialog_support.submit` o código usava `validator.formSubmitted` como trava: após o primeiro submit (mesmo com validação falhando), `formSubmitted` ficava `true` e os cliques seguintes faziam **nada**. Além disso, `validator.valid() && $('#submit').prop('disabled', true)` desabilitava o botão para sempre se a validação passasse (ex.: resposta `success:false` do servidor mantinha o modal aberto com botão morto).
- **Solução:** no `submit` action — resetar `validator.formSubmitted = false` antes de `form.submit()`, guardar duplo-clique via `!$('#submit').prop('disabled')`, e no `submit_handler` (resposta do ajaxSubmit) **re-habilitar** `#submit` (`prop('disabled', false)`) sempre que a resposta voltar. Aplicado na fonte (`public/js/manage_tables.js`), nos cópias compiladas (`resources/js/manage_tables-*.js`) e no bundle minificado (patch cirúrgico, sintaxe validada com `node --check`). Vale para TODOS os modais que usam dialog_support (items, customers, suppliers, etc.).
- **Aplicado em:** 31/jul/2026. Commitado em `fdfa8fea9`.

### 11. Sistema inteiro aceita `.` e `,` como separador decimal (+ troco no modal)
- **Arquivos:** `app/Helpers/locale_helper.php` (`parse_decimals`) + `app/Views/sales/register.php`
- **Sintoma A (modal checkout):** em vendas ≥ R$ 1.000 o parse de `total_venda` quebrava — `to_currency` usa separador de milhar, então `"R$ 1.234,56"` virava `1.234` (parseFloat lia o ponto como decimal). O modal mostrava TOTAL/RESTANTE como `R$ 1,23` numa venda de R$ 1.234,56; o caixa digitava o que via, a venda finalizava sub-paga ("restante que fica") e o troco não aparecia. `postQuickFinish` não valida sub-pagamento.
- **Sintoma B:** `<input type="number">` engolia vírgula em vários browsers (`50,50` → `505` ou vazio).
- **Solução:** `parseCurrency()` + `fmtMoney()` no script inline do checkout (parse robusto: último separador com 1-2 dígitos é o decimal; formatação com milhar). Input trocado para `type="text" inputmode="decimal"`. `parse_decimals()` (servidor, cobre TODOS os módulos: itens, carrinho, pagamentos, atributos) agora normaliza `.` e `,` para `en_US` antes do `NumberFormatter`, com a mesma heurística. Validação do `addDiversos` usa `parseCurrency`.
- **Troco visível:** o `troco_display` foi movido do fim do `.modal-body` (ficava abaixo da lista de pagamentos, exigia scroll) para o **topo do modal**, logo abaixo do TOTAL/RESTANTE, com `min-height: 28px` para não pular o layout quando aparece/some.
- **Observação:** `public/js/checkout.js` é código morto (contém `<?= ?>` não renderizado; nenhum view o carrega) — pode ser removido no futuro.
- **Aplicado em:** 31/jul/2026. Commitado em `61396c8a0` + `0f5b0894a`.

### 12. Horário das vendas gravado com 1h de atraso (timezone UTC-4)
- **Arquivos:** seeds `app/Database/Migrations/sqlscripts/initial_schema.sql`, `app/Database/database.sql`, `app/Database/tables.sql` (linha 24) + dados em `ospos_app_config` (prod e teste)
- **Sintoma:** vendas em sales/manage marcavam 1h atrás (ex.: 08:50 quando foi 09:50). Afetava TODAS as vendas desde a primeira (21/mar/2026) — 4735 vendas.
- **Causa raiz:** seed default `timezone = 'America/New_York'` (UTC-4 no verão) gravado no `ospos_app_config`. O `Load_config.php:52` (`date_default_timezone_set($config->settings['timezone'] ?? ini_get('date.timezone'))`) sobrescreve o `appTimezone = America/Sao_Paulo` do CI4, então `Sale::save_value()` grava `sale_time` via `date('Y-m-d H:i:s')` em UTC-4. O `payment_time` (coluna `timestamp` do MySQL) ficava CORRETO — serviu de referência para a correção.
- **Solução:**
  1. `UPDATE ospos_app_config SET value='America/Sao_Paulo' WHERE key='timezone'` (prod + teste) — resolve daqui pra frente.
  2. Correção retroativa (prod): `ospos_sales.sale_time` e `ospos_inventory.trans_date` ajustados +1h usando `MIN(payment_time)` por venda como referência. 0 divergências restantes.
  3. Seeds do fork corrigidos para `America/Sao_Paulo` (evita recorrência em instalação nova / rebuild de DB).
  4. Backups: `ospos_sales_bak_tz` e `ospos_inventory_bak_tz` (tabelas temporárias no DB prod, podem ser dropadas).
- **Observação:** `postSave` (editar venda via sales/form) usa a data digitada no formulário — não é afetado por este bug.
- **Aplicado em:** 1/ago/2026 (dados + seeds; ainda não commitado).

### 13. Botão "Imprimir Venda" em sales/manage dava 500 (visibilidade de propriedade)
- **Arquivo:** `app/Controllers/Printer.php` (fork, prod e teste)
- **Sintoma:** ao clicar em "Imprimir Venda" em `sales/manage`, o botão não imprimia. Log CI4: `CRITICAL - ErrorException: Access level to App\Controllers\Printer::$employee must be protected (as in class App\Controllers\Secure_Controller) or weaker [Method: POST, Route: printer/quickPrint] in APPPATH/Controllers/Printer.php on line 21`. O guardrail pegou o incidente; usuário precisou imprimir via script CLI.
- **Causa raiz:** `Printer::$employee` declarada `private`, mas `Secure_Controller` já declara `protected Employee $employee`. PHP proíbe reduzir a visibilidade de propriedade herdada (compile-time) → toda requisição que carregava a classe `Printer` (quickPrint, printReceipt, test) morria com 500.
- **Solução:** trocar para `protected Employee $employee;` (mesma visibilidade do pai). Aplicado em fork, prod e teste.
- **Observação:** os scripts CLI de impressão (`/tmp/opencode/print_receipt.php`) precisam setar `bcscale(max(2, totals_decimals() + tax_decimals()))` manualmente — o evento `Load_config.php:54` (que seta bcscale no web) não roda em bootstrap CLI; sem isso `bcmul`/`bcadd` truncam decimais (ex.: 22.50 → 22.00) e a impressão sai com valores errados.
- **Aplicado em:** 1/ago/2026. Commitado em `fdfa8fea9`. [Ajustar commit/hash quando commitar]

### 14. Impressora: "Permission denied" em /dev/usb/lp0 pelo Apache (regra udev errada)
- **Arquivo:** `/etc/udev/rules.d/99-epson-tm20.rules`
- **Sintoma:** após o fix #13, o botão Imprimir Venda respondia `fopen(/dev/usb/lp0): Failed to open stream: Permission denied`.
- **Causa raiz:** a regra udev usava `ATTR{idVendor}`/`ATTR{idProduct}` (match só nos atributos do próprio device usbmisc), mas `idVendor`/`idProduct` ficam na interface USB **pai** → a regra nunca casava e o node era criado como `root:lp` (grupo padrão 50-udev-default), e o www-data não pertence ao grupo `lp`.
- **Solução:** trocar para `ATTRS{idVendor}`/`ATTRS{idProduct}` (com `S` — busca no device e nos pais) nas 4 linhas, `udevadm control --reload-rules` e `udevadm trigger --subsystem-match=usbmisc`. Node agora fica `root:www-data` 0660. Confirmado que www-data abre p/ escrita. Permanente (sobrevive a reboot/replug via udev).
- **Aplicado em:** 1/ago/2026.

### 15. Após imprimir em sales/manage, produtos da venda apareciam em sales/add (sessão poluída)
- **Arquivo:** `app/Controllers/Printer.php` (`loadSaleData`, fork, prod e teste)
- **Sintoma:** depois de clicar "Imprimir Venda" em sales/manage, ao abrir sales/add (register) os itens da venda impressa já estavam no carrinho.
- **Causa raiz:** `loadSaleData()` roda `$this->sale_lib->copy_entire_sale($saleId)`, que carrega a venda inteira na **sessão do usuário** (carrinho, pagamentos, sale_id, etc.) — e nunca limpava. A sessão persistia entre requisições, então o register herdava o carrinho.
- **Solução:** `$this->sale_lib->clear_all()` no final de `loadSaleData()` (depois de montar `$data`, antes do `return`), esvaziando o carrinho/pagamentos/`sale_id` da sessão. Aplicado em fork, prod e teste.
- **Observação:** diferente de `postUnsuspend` (Sales.php:1417-1421), que usa `clear_all()` + `copy_entire_sale()` DE PROPÓSITO para carregar venda suspensa no register — no Printer o carregamento é só para computar/imprimir os dados, então a sessão deve ser limpa.
- **Aplicado em:** 1/ago/2026.

### 16. Lote de UX: tutoriais, receipt, liveclock, feedback de produto, export CSV
- **Arquivos:** vários (ver abaixo) — aplicado no fork + deploy-teste (`/var/www/html/pos-test`); ainda NÃO commitado nem no prod.
- **Tutoriais quebrados (caça-de-bugs):** `app/Views/partial/tutorial.php` lia `window._tutorialSteps` no parse do script, mas a view só define depois → array sempre vazio. Agora `window.startTutorial()` relê `window._tutorialSteps || []` no clique (notifica se vazio). `sales/register.php` tinha step apontando `#item_search` (não existe) → corrigido para `#item`. Adicionados `_tutorialSteps` em `items/manage.php`, `sales/manage.php`, `people/manage.php` (usado por customers/suppliers/persons). Validadas no teste: customers=3 passos, items=4, sales/manage=5, register=5.
- **"in stock" em inglês:** `app/Views/receivings/receiving.php:140` — `'[' . to_quantity_decimals($item['in_stock']) . ' em ' . $item['stock_name'] . ']'`.
- **Receipt:** `app/Views/sales/receipt.php` — removido bloco `printdoc()`/`show_print_button`; térmico renomeado para "Imprimir Nota".
- **Typo:** `app/Language/pt-BR/Sales.php` — "Registar Venda" → "Registrar Venda"; deduplicadas as 3 chaves `item_not_found` (sobra "Produto não cadastrado no sistema.").
- **Feedback de produto inexistente:** `app/Controllers/Sales.php` `postAdd` — quando `add_item()` falha (só retorna false se item não existe), mostra `Sales.item_not_found` em vez do genérico `unable_to_add_item` (campo vazio mantém o genérico).
- **Liveclock com dia da semana:** `app/Views/partial/header.php` (PHP `$dias_semana[(int)date('w')]` inicial) + `header_js.php` `update_clock()` (JS `dias_semana[now.getDay()]`) — ex.: "Sábado, 01/08/2026 12:49:22".
- **Export CSV em relatório gráfico:** `app/Views/reports/graphical.php` — botão "Exportar para CSV" (`Common.export_csv`) gera CSV client-side a partir de `labels_1`/`series_data_1` (BOM p/ Excel, `;` como separador, decimal `pt-BR`). Relatórios tabulares já exportavam via bootstrap-table (`tabular.php`).
- **Home clicável (irregular/top itens):** `app/Controllers/Home.php` — selects de `top_items` e `stock_alerts` passam a incluir `items.item_id`. `app/Views/home/home.php` — cards de alerta viram `<a class="dash-alert-card" href="items?edit_item={id}">` e linhas do Top 5 ficam clicáveis (`data-href` + nome como link). `app/Views/items/manage.php` — ao chegar com `?edit_item={id}`, cria um `.modal-dlg` temporário (`items/view/{id}`) e dispara o clique, abrindo o modal de edição do item direto; limpa a URL via `history.replaceState`.
- **Tabela de items reestruturada (editar visível + menos colunas):** `app/Helpers/tabular_helper.php` — colunas de ação (editar/inventário/estoque) movidas para o INÍCIO (depois do checkbox) em `get_items_manage_table_headers()`, então o botão editar fica visível sem rolagem horizontal; `last_modified` e `item_pic` escondidas por padrão mas reativáveis via seletor de colunas (`visible:false` + `switchable`). `transform_headers()` ganhou suporte a `class` e `visible` por coluna; classes `items-col-*`/`sales-col-*` alinham dinheiro à direita em mono e ID centralizado. `public/css/modern.css` — regras genéricas `nth-child` (erradas para items) substituídas por regras escopadas em `#table_holder.items-manage`.
- **Aplicado em:** 1/ago/2026 (teste validado em `localhost:8080`). Pendente: commit no fork + deploy prod.

### 17. Editar item em /items: Enviar "não fazia nada" (checkNumeric 404)
- **Arquivo:** `app/Config/Routes.php` (+ `app/Controllers/Secure_Controller.php` já tinha o método)
- **Sintoma:** ao editar qualquer item em `/items` e clicar Enviar, nada acontecia (modal ficava aberto, sem erro no log CI4). O problema já tinha sido reportado como "botão morto" (fix #10) — mas o paliativo não resolvia.
- **Causa raiz:** o jQuery Validate dos formulários (items, config, expenses) usa regras `remote: "checkNumeric"` nos campos numéricos (cost_price, unit_price, quantity_*, receiving_quantity, reorder_level). Após o audit setar `setAutoRoute(false)` no Routes.php, o endpoint `items/checkNumeric` ficou SEM rota → `GET /items/checkNumeric?cost_price=...` retornava **404**. O remote rule falha silenciosamente (campo marcado inválido), `validator.valid()` nunca passa e o submitHandler nunca roda → "nada acontece". O método `getCheckNumeric()` (que ecoa `true`/`false`) sempre existiu em `Secure_Controller`, herdado por todos os controllers.
- **Solução:** adicionadas as rotas GET ausentes:
  - `items/checkNumeric` → `Items::getCheckNumeric`
  - `config/checkNumeric` → `Config::getCheckNumeric`
  - `expenses/checkNumeric` → `Expenses::getCheckNumeric`
- **Verificação:** `curl /items/checkNumeric` passou de 404 → 302 (redireciona pro login quando sem sessão). Não confundir 302 com erro: método inexistente devolve 404.
- **Aplicado em:** 1/ago/2026. Aplicado em prod e teste (apenas o Routes.php foi sincronizado; o lote UX do fix #16 NÃO foi pro prod).

### 18. Tela branca (404 body vazio) + produtos de venda vazando para sales/add
- **Arquivos:** `app/Controllers/Guardrail.php` (fork, prod e teste)
- **Sintoma A:** `GET /sales/reopen/4823` (URL digitada/colada no browser) abria TELA BRANCA — resposta `HTTP 404` com body de 0 bytes.
- **Sintoma B:** ao alterar forma de pagamento ou cliente em `sales/manage` (modal do lápis), os produtos da venda editada apareciam no carrinho de `sales/add`. O `js-errors.log` mostrava `Uncaught SyntaxError: Unexpected end of input` com `src=http://localhost/sales/manage` (resposta vazia avaliada como script pelo jQuery).
- **Causa raiz:** `Routes.php:508` tem `$routes->set404Override('\App\Controllers\Guardrail::notFound')`. O método fazia `$this->response->setBody(...)` mas retornava `void` — o `CodeIgniter::gatherOutput()` (~linha 1041 do framework 4.7.2) sobrescreve o body da response com o output buffer (VAZIO) → qualquer 404 virava resposta de 0 bytes. XHRs que recebiam esse 404 vazio quebravam o JS da página (SyntaxError), deixando o modal de edição em estado inconsistente que vazava a venda carregada para a sessão do register.
- **Solução:** em `notFound()`, usar `echo` do HTML (cai no output buffer que o `gatherOutput()` coleta) em vez de `setBody()`. Todos os 404 agora retornam body HTML (prod: 168 bytes; teste: ~28KB com debugbar). Verificado com `curl -H "Accept: text/html" http://localhost/sales/reopen/4823`.
- **Verificação do Sintoma B:** percorridos via curl com sessão real (login com `admin`/`pointofsale` — NOTA: a senha no banco é `pointofsale`, não `Arroz123@` como constava na doc): `sales/edit/{id}`, `POST sales/save/{id}`, `sales/row/{id}`, `sales/search`, `sales/receipt/{id}`, `sales/invoice/{id}`, `sales/getSaleItems`, `sales/getPaymentSummary`, `POST printer/quickPrint` — em TODOS a sessão permaneceu `sales_cart|a:0:{}`. `postSave` e `getEdit` não tocam a sessão; `getSendPdf`/`getSendReceipt`/`getReceipt`/`getInvoice` limpam via `clear_all()`; `Printer::loadSaleData` limpa (fix #15).
- **Aplicado em:** 3/ago/2026. Commitado em `e68af7be4` e pushed (`merge-staging`). Sincronizado em prod e teste antes do commit.

### 19. Tabela do carrinho em sales/add: foto + desconto visíveis, colunas estreitas, remoção de código morto
- **Arquivos:** `app/Views/sales/register.php` (fork, prod e teste) + sincronização do lote de imagem (footer.php, Sale_lib.php, items/form.php, items/manage.php, modern.css, Items lang)
- **Sintoma:** usuário não via a **foto** nem o botão de **desconto** (R$/% toggle) na tabela do carrinho de vendas; colunas de preço/quantidade largas demais para a info que carregam; presença de "sessão editar venda" inútil e botão "shortcuts" nunca usado.
- **Causa raiz:**
  1. **Foto:** o lote de imagem (thumbnail `cart-item-img` + modal `itemImageViewer` + `pic_filename` no `Sale_lib::add_item`) estava aplicado só no fork/teste — o PROD ainda não o tinha (diferença confirmada por `diff`).
  2. **Desconto:** `#register td:nth-child(6) { overflow: hidden }` cortava o toggle R$/% na coluna discount.
  3. **Editar venda:** `$editing_sale_id` é passado à view (`register.php:182-187, 572-580, 1173, 1230-1231`) mas **nunca é setado** por nenhum controller (`grep editing_sale_id` → só a view) — código morto.
  4. **Shortcuts:** botão `show_keyboard_help` (linha 531) e atalho `Alt+9` (case 57) abriam `sales/help.php` — nunca usados.
- **Solução:**
  - Larguras do carrinho: item_name 35%→**40%**, price 9%→**6%**, quantity 9%→**7%**, total 9%→**7%** (discount mantém 13%).
  - `overflow: hidden` → `visible` na coluna 6 (discount) para o toggle R$/% não ser cortado.
  - Removidos o bloco de alerta "Editando Venda", o botão ATUALIZAR VENDA (vira sempre FINALIZAR VENDA), e os ternários `editing_sale_id` do modal checkout.
  - Removidos o botão "Shortcuts" e o case `Alt+9`.
  - Sincronizado o lote completo de imagem (8 arquivos) do fork → prod com `cp` arquivo-a-arquivo (rsync com múltiplos destinos não funciona — só o último arg é destino; usar pares únicos `cp`).
- **Atenção rsync:** `rsync src1 dst1 src2 dst2 ...` NÃO copia em pares — todos os args são fontes e só o último é destino. Um comando assim despejou `.php` de views/libs em `public/css/` (limpo depois).
- **Verificação (prod, `localhost`):** login `admin`/`pointofsale`; `POST /sales/add` com item 10113 (foto `10113.png`) → linha do carrinho contém `cart-item-img` + `data-img-view`; página sem `editing_sale_id`/`show_keyboard_help`; larguras 40/6/7/13/7; modal `#itemImageViewer` presente. NOTA: item 3913 está `deleted=1` no prod — não entra no carrinho por `include_deleted=false` no `Sale_lib::add_item`/`Item::get_info_by_id_or_number`.
- **Aplicado em:** 4/ago/2026. Commitado em `dda24c598` e **pushed** (5/ago/2026).

### 20. Foto do produto GRANDE em cima do código no carrinho de vendas (sales/add)
- **Arquivos:** `app/Views/sales/register.php` + `public/css/modern.css` (fork, prod e teste)
- **Sintoma:** a thumbnail de 36px era pequena demais e ficava ao lado do nome — usuário queria a foto grande, acima do código, para conferir o produto na venda.
- **Solução:**
  - Células `item_number` + `item_name` unificadas em `<td colspan="2" class="cart-product-cell">` com wrapper `.cart-prod`: foto **110×110px** (`.cart-item-img-big`, hover com zoom), nome (`.cart-prod-name`, **15px bold**), código de barras (`.cart-prod-code`, mono) e estoque/status (`.cart-prod-stock`, badge **9px**) empilhados.
  - **Coluna "Editar Venda" (update) REMOVIDA** — os edits de preço/qtde/desconto salvam no blur via handler global `change` (`$(this).closest('form').submit()` — o antigo `parents('tr').prevAll('form:first')` NÃO achava o form, então editar preço só funcionava pelo botão). Descrição não é mais editável na linha (célula "Sem descrição" removida); preservada via `form_hidden('description', ...)` no 1º `<td>` para o `editItem` não perdê-la.
  - Segunda linha só existe para itens TEMP (descrição) e serializados (label Serial + input, `colspan="5"`); itens comuns ficam com 1 linha só.
  - Larguras (7 colunas): delete 4% / item_number 6% / item_name **58%** / price **4%** / quantity **8%** / discount 13% / total 7%.
  - Stepper de qty compacto: input **48px × 24px**, botões −/+ 24px com `padding: 2px 5px`, `.qty-stepper { max-width: 108px }`; regra `#register td > .form-control` (28px) para inputs de preço.
  - Toggle de desconto: `overflow: visible` cobre `nth-child(5)` E `nth-child(6)`; mono aplicado em `nth-child(3..6)`.
- **Verificação (prod, `localhost`):** sessão real + `POST /sales/add` item 10113 → linha única com `cart-product-cell`, `cart-item-img-big`, `cart-prod-name/code/stock`; SEM `Editar Venda`/`glyphicon-refresh`/`Sem descrição`; `POST sales/editItem/1` com price/quantity altera o carrinho e preserva a descrição; `php -l` OK nos 3 ambientes.
- **Aplicado em:** 5/ago/2026. Commitado em `339f1bfda` + refinamentos (colunas/sem update) em commit pendente (`merge-staging`). Sincronizado em prod e teste.

### 21. Inventory-service: fix Decimal no store_sync + photo write-back no OSPOS
- **Arquivos:** `/home/ismael/inventory-service/` — `app/services/store_sync.py`, `app/api/v1/store.py`, `app/services/ospos_client.py` (novo), `app/services/duplicate_rule.py` (novo), `app/config.py`, `app/models/store_product.py`
- **Sintoma:** sync falhava ao ligar `Decimal` do MySQL (coluna `quantity` DECIMAL) na tabela SQLite (`StatementError ... type Decimal is not supported`), derrubando a sincronização com a loja.
- **Causa raiz:** o bind de `stock` usava o valor cru do MySQL; SQLite não aceita `Decimal` como parâmetro.
- **Solução (combo do lote de photo-capture + sync):**
  - `store_sync.py`: `stock=int(row[6] or 0)` (coerce para int, unidades inteiras); query reescrita com `FROM ospos_items AS items` + subselect de estoque real em `ospos_item_quantities` (location_id=1) em vez de `receiving_quantity`; campo `last_modified` propagado ao `StoreProduct`; fallback de imagem para uploads locais (`product_{local_id}.png` etc.) que não existem no OSPOS; `_dedupe_store()` garante 1 produto visível por SKU.
  - `store.py`: `upload_product_image` faz write-back no OSPOS (copia foto para `item_pics/{item_id}{ext}` via `ospos_client.resolve_photo_target()` — redireciona para o item ativo quando o mapeado está `deleted` — + `set_pic_filename()`); `register_scan` resolve SKU duplicado deprioritizando itens deletados.
  - `config.py`: `ospos_uploads_dir` novo setting.
- **Verificação:** `systemctl restart inventory.service` OK; `POST /v1/store/sync?mode=full&min_stock=0` → `{"status":"completed",created:5,updated:8485,skipped:0,errors:0}`.
- **Aplicado em:** 5/ago/2026. Commitado em `e52fc03` (branch `main`, ainda não pushed). NOTA: `main.py`, `app/api/v1/observability.py` e `app/utils/security.py` têm alterações de OUTRA frente (swarm/agent + relaxamento de API key) que NÃO foram commitadas — revisar antes de incluir.

### 22. Rotacionar foto do produto em items (fotos capturadas de lado)
- **Arquivos:** `app/Controllers/Items.php` (novo `postRotateItemImage`), `app/Config/Routes.php` (rota POST `items/rotateItemImage/(:num)`), `app/Views/items/form.php` (botões ⟲/⟳ + JS), `app/Language/{pt-BR,en}/Items.php` (chaves `rotate_image`/`rotate_left`/`rotate_right`/`rotate_fail`/`image_not_found`)
- **Sintoma:** foto capturada pelo Loja Capture subia deitada (orientação errada) e não havia como corrigir no OSPOS.
- **Solução:**
  - Botões **⟲ (ccw)** e **⟳ (cw)** ao lado de "Substituir imagem"/"Remover imagem" no form de items, visíveis só quando há foto salva (`.fileinput-exists`).
  - `postRotateItemImage()` lê `pic_filename` do item, localiza o arquivo em `uploads/item_pics/`, gira **90°** via GD (`imagerotate`, `match` por extensão png/gif/webp/jpeg), salva por cima preservando formato (`imagepng(…,9)` / `imagejpeg(…,90)` / webp/gif) e `chmod 664` (mantém write-back do inventory-service funcional).
  - Thumb `_thumb.{ext}` (gerado pelo `getPicThumb`) é apagado se existir — a thumbnail da grid regenera sozinha.
  - JS faz `POST items/rotateItemImage/{id}` com `direction` e atualiza o `src` do preview com cache-buster (`?t=Date.now()`); botão desabilita durante a chamada; erro → `alert`.
  - Resposta JSON com `Content-Type: application/json` (padrão do fix #6).
- **Verificação (teste, `localhost:8080`, login `admin`/`Arroz123@`):** botões renderizam no form; `POST items/rotateItemImage/10113` com `direction=cw` → `{"success":true}` e PNG troca de 2993×4000 → 4000×2993; `ccw` de volta restaura dimensões; item sem imagem → `{"success":false,"message":"Não foi possível girar a imagem."}`; `php -l` OK; sem erros no log CI4. Cópia do teste restaurada do prod após o teste.
- **Aplicado em:** 5/ago/2026. Commitado em `7b67683b2` (`merge-staging`, ainda não pushed). Sincronizado em prod e teste.
- **Fix de permissão (6/ago/2026):** no teste real o botão dava **500** — `imagepng(4387.png): Failed to open stream: Permission denied`. Causa raiz: fotos gravadas pelo write-back do inventory-service ficam `ismael:ismael 664` (chmod 664 NÃO basta se o grupo não é `www-data`), e o Apache não pode escrever no arquivo. Solução em 2 frentes:
  1. `Items::postRotateItemImage` grava em `{arquivo}.rot` (temp, o Apache consegue criar via permissão de escrita do diretório `item_pics` = `ismael:www-data 775`) e faz `rename()` por cima do original — funciona para QUALQUER dono do arquivo. Commitado em `6ab2ac356`.
  2. `store.py` (write-back) agora faz `os.chgrp(dest, "www-data")` após o chmod — futuras fotos do Loja Capture já nascem `www-data`-graváveis. Commitado em `b4601de` (inventory-service). `os.chgrp` funciona porque ismael é dono do arquivo e pertence ao grupo `www-data`.
  3. Arquivos legados `ismael:ismael` em `uploads/item_pics` (4387.png, 10192.png, 9935.png, 9818.png) corrigidos com `chgrp www-data` (prod e teste).
  - Verificação prod: `POST items/rotateItemImage/4387 direction=cw` → 200 `{"success":true}`, dimensões (4000,3000)→(3000,4000), arquivo vira `www-data:www-data 664`; `ccw` restaura. JS atualiza o preview na hora com cache-buster `?t=Date.now()`.

## Auto-Heal / Guardrails (watcher de auto-conserto)

Sistema de vigilância que mantém a produção no ar quando ocorrem erros conhecidos, enquanto registra incidentes para correção posterior.

### Arquivos — `/home/ismael/guardrails/`
| Arquivo | Função |
|---------|--------|
| `auto-heal.sh` | Watcher (cron) — varre logs, classifica erros, aplica paliativo e notifica |
| `heal_sessions.php` | Sanitiza `payment_amount` corrompido nas sessões (lê credenciais do `.env` do prod) |
| `incidents.log` | Histórico de TODOS os erros capturados (para revisão/correção posterior) |
| `heal.log` | Ações de auto-conserto executadas |
| `.state` | Última linha processada por fonte (`KEY\|FILE\|LINE`) |
| `.seen` | Hashes de erros já notificados (dedupe de notificações) |

### Cobertura
- **Log CI4** (dia atual): linhas `CRITICAL` e `ERROR` — `/var/www/html/pos/writable/logs/log-YYYY-MM-DD.log`
- **Apache** `/var/log/apache2/error.log`: níveis `error`, `crit`, `alert`, `emerg`
- **Erros de JS do browser** — `/var/www/html/pos/writable/logs/js-errors.log` (novo 31/jul/2026): captura falhas client-side que não geram log no servidor (ex.: botão que "não funciona"). O browser reporta via `POST /guardrail/js-error` (trap global injetado no `partial/footer.php`, presente em todas as páginas autenticadas).

### Comportamento
| Tipo de erro | Ação |
|--------------|------|
| bcmath `not well-formed` (erro conhecido) | **Auto-conserta** via `heal_sessions.php` + notifica |
| Erro desconhecido (CRITICAL/ERROR CI4, erro Apache, erro JS) | Registra em `incidents.log` + **notifica 1x** (dedupe por hash) |
| Queries SQL com erro no log CI4 | Só registra (evita spam do dashboard) |

### Pipe de erros de JS (endpoint + trap)
- **Trap** no `app/Views/partial/footer.php`: handlers globais `error` + `unhandledrejection` enviam `{message, stack, source, line, col, url, ua}` via `navigator.sendBeacon` (rate-limit 1/seg) para `POST /guardrail/js-error`.
- **Endpoint** `app/Controllers/Guardrail.php` (`Guardrail::jsError`), rota `$routes->post('guardrail/js-error', ...)` — público (sem auth), grava 1 JSON por linha em `writable/logs/js-errors.log`.
- O `auto-heal.sh` lê `js-errors.log` como fonte `js` (estado `.state` com chave `js`), registra incidente + notifica (extrai `msg` e `url` do JSON via gawk).
- **Log de falhas de save**: `app/Controllers/Items.php` `postSave` agora faz `log_message('error', ...)` quando o save falha (o guardrail pega no log CI4).

### Agendamento e execução
- Cron do usuário: `*/2 * * * * /bin/bash /home/ismael/guardrails/auto-heal.sh`
- Consumo: ~0,03s e ~4 MB por execução (leitura incremental via `.state`); PHP do heal só roda quando há erro bcmath
- Permissões: ismael está nos grupos `www-data` (lê log CI4) e `adm` (lê log Apache). `flock` impede execução concorrente. A leitura do log CI4 usa `sg www-data` para funcionar tanto no cron quanto em shell interativo (o grupo pode não estar carregado na sessão).
- Notificação desktop: `notify-send` com `DISPLAY`/DBUS detectados da sessão do usuário

### Como adicionar novo paliativo
Editar o `case "$MSG"` em `auto-heal.sh`:
```bash
*padrão_do_erro*)
    log_incident "origem line=$LINE_NO [$LEVEL] $MSG"
    # ... chamar script de conserto e registrar em heal.log ...
    notify_once "auto-heal" "$RESULT"
    ;;
```
Erros desconhecidos já são alertados automaticamente — só os **paliativos** precisam ser codificados manualmente.

### Observação de manutenção
- `.state` por fonte: se o `.state` for apagado, a próxima execução reprocessa o log inteiro do dia (heurísticas idempotentes, sem efeito colateral além de re-registrar incidentes).
- O watcher cobre o **dia atual** do log CI4; arquivos de dias anteriores só são revistos manualmente.
- Testes ponta-a-ponta validados em 31/jul/2026 (sessão com `9.9E+7` → `99000000`, erro Apache e erro desconhecido capturados).
- Testes JS-errors validados em 31/jul/2026: `POST /guardrail/js-error` grava JSON em `js-errors.log`, `auto-heal.sh` registra incidente + avança `.state` (fonte `js`), endpoint idempotente (2 execuções seguidas sem duplicatas).

### 23. Feedback em tempo real de fotos salvas (Loja Capture → OSPOS, PC + celular)
- **Arquivos:** inventory-service `app/api/v1/store.py` (`_log_photo_event`, `_recent_photos`, `GET /v1/store/photos/recent`, `WS /v1/store/photo/ws`, broadcast no `upload_product_image`), `app/main.py` (mount `/static`), `app/config.py` + `.env` (CORS p/ `http://localhost`, `127.0.0.1`, `192.168.15.6`), `static/photos.html` (novo, página mobile). OSPOS: `app/Views/items/manage.php` (fork, prod e teste).
- **Sintoma:** ao capturar foto no celular, não havia confirmação de que a foto foi salva no sistema — o usuário não sabia se/precisava conferir manualmente.
- **Solução:**
  - **Servidor:** cada upload de foto grava 1 linha JSON em `data/photo_uploads.jsonl` (`{ts, product_id, product_name, ospos_item_id, pic_filename, status: ok/failed, error}`) e faz broadcast. `_recent_photos()` lê só o tail (máx 64KB, inverso) — leve. `WS /v1/store/photo/ws` envia o último evento na conexão (`last: true`) e depois só broadcasts. `GET /v1/store/photos/recent?limit=N` serve a página mobile.
  - **PC (tela Items):** JS no `items/manage.php` abre WS `ws://{hostname}:8000/v1/store/photo/ws`; ao receber evento → toast verde "Foto salva no sistema: {nome}" + `table_support.refresh()` (a thumb aparece na grid na hora). Falha → toast vermelho. Dedupe por `ts|product_id`; reconnect com backoff; se o WS falhar 3x, cai em polling leve a cada 10s (`/v1/store/photos/recent?limit=3`). CORS adicionado para o fallback.
  - **Celular:** `http://192.168.15.6:8000/static/photos.html` — página simples que mostra os últimos 6 uploads ✓/✗, atualiza a cada 4s, bom para deixar aberta durante as capturas.
- **Verificação (5/ago/2026):** `py_compile` OK, `php -l` OK (fork/prod/teste), `node --check` no JS; `curl /v1/store/photos/recent` → 200; teste de WS via `websockets` (venv): ping/pong OK e evento fake no JSONL foi entregue na conexão (`{"type":"photo","last":true,...}`); log fake removido; CORS validado p/ `localhost` e `192.168.15.6`. `.env` NÃO é commitado (CORS fica lá + no default do `config.py`).
 - **Aplicado em:** 5/ago/2026. Pendente de commit (fork + inventory-service).
- **Sync total para outro PC (6/ago/2026):** `GET /v1/store/sync-total?limit=&offset=&include_deleted=&since=YYYY-MM-DD HH:MM:SS` (headers `X-Total-Count`/`X-Limit`/`X-Offset`) lê direto do MySQL `ospos_items` via `ospos_client.fetch_items_total` (pool aiomysql), pagina sobre `item_id`. `GET /v1/store/ospos-item-images/{filename}` serve fotos do write-back (`uploads/item_pics`) pela LAN. Outro PC consome com `curl http://192.168.15.6:8000/v1/store/sync-total?limit=1000&offset=N` até `X-Total-Count`. CORS inclui `192.168.15.6`. Validado: total 10113, item 4974 retorna `image_url` e PNG 200 (6,3MB).

### 24. App Android mostrava venda de R$48 como R$40 nas "Últimas vendas" (`discount_type` invertido)
- **Arquivos:** inventory-service `app/services/ospos_client.py` — 3 ocorrências do mesmo CASE: `fetch_dashboard_summary` top-5 revenue (~linha 238), `fetch_new_sales` (~366, usado na notificação WS `sale`) e `fetch_recent_sales` (~401, endpoint `/v1/dashboard/sales/recent`).
- **Sintoma:** venda de R$48 (item de R$60 com 20% de desconto) aparecia como **R$40** na lista "Últimas vendas" do app. Totais do dashboard ficavam corretos (somam pagamentos).
- **Causa raiz:** no OSPOS, `app/Config/Constants.php:146-147` define `PERCENT = 0` e `FIXED = 1`. As queries tratavam `discount_type=1` como percentual (`.../100`) e `discount_type=0` como valor fixo (`price - discount`) — **INVERTIDO**. Para desconto percentual (0), calculava 60−20=40 em vez de 60−20%=48.
- **Solução:** trocados os ramos nas 3 queries: `discount_type=1` (FIXED) → `si.quantity_purchased * (si.item_unit_price - si.discount)`; `ELSE` (PERCENT) → `si.quantity_purchased * si.item_unit_price - ROUND(si.quantity_purchased * si.item_unit_price * si.discount / 100, 2)`.
- **Verificação (15/ago/2026):** `py_compile` OK; `curl /v1/dashboard/sales/recent` retorna 48.00 para a venda 5278 (60 − 20%); `systemctl restart inventory.service`.

### 25. `payment_type` gravado HTML-encodado (`Cart&atilde;o D&eacute;bito` em vez de `Cartão Débito`)
- **Arquivos:** OSPOS `app/Controllers/Sales.php` (`postAddPayment` linha ~233 e `payment_type_new` linha ~1247 — fork, prod e teste); inventory-service `app/services/ospos_client.py` (`payment_summary`, defesa no backend); dados em `ospos_sales_payments`.
- **Sintoma:** no resumo por forma de pagamento do app (período mês), aparecia um grupo "Cart&atilde;o D&eacute;bito ... de R$2,00" separado do Cartão Débito correto.
- **Causa raiz:** `postAddPayment` usava `FILTER_SANITIZE_FULL_SPECIAL_CHARS` no `payment_type`, que **HTML-encodeia** o valor antes de gravar ("Cartão" → `Cart&atilde;o`). 7 registros afetados (16/mai a 12/ago/2026; último: venda 5157, R$2,00). `postQuickFinish`/`addDiversos` **não** sanitizam (por isso a maioria dos registros estava correta). O `payment_summary` do inventory-service agrupava e devolvia o texto cru.
- **Solução (3 frentes):**
  1. **Dados:** `UPDATE ospos_sales_payments SET payment_type='Cartão Débito' WHERE payment_type='Cart&atilde;o D&eacute;bito'` (e o mesmo p/ `Cart&atilde;o Cr&eacute;dito`). Agora 5 tipos limpos: Dinheiro, Cartão Débito, PIX, Cartão Crédito, Fiado.
  2. **OSPOS (prevenção):** `html_entity_decode(...)` envolvendo o `getPost(...)` nas 2 linhas; `cp` arquivo-a-arquivo p/ `/var/www/html/pos` e `/var/www/html/pos-test`; `php -l` OK.
  3. **Backend (defesa):** `html_unescape(payment_type)` no `payment_summary` — entidades nunca mais vazam pro app, mesmo com dados legados/futuros.
- **Verificação (15/ago/2026):** resumo mês retorna `Cartão Débito 188 / R$6003,20`, `PIX 117 / R$4006,95`, etc. Sincronizado em prod e teste.

### 26. App Android — notificações em tempo real confiáveis em background (foreground service + WS fix)
- **Arquivos:** `/home/ismael/ospos-dashboard-app/` — `DashboardApplication.kt` (singleton `repository`), `services/LiveUpdatesService.kt` (**novo**), `ui/MainActivity.kt` (usa o singleton + `startForegroundService`), `AndroidManifest.xml` (permissões `FOREGROUND_SERVICE`, `FOREGROUND_SERVICE_DATA_SYNC`, `WAKE_LOCK` + `<service android:name=".services.LiveUpdatesService" android:foregroundServiceType="dataSync">`), `res/values/strings.xml` (canal `live_updates` IMPORTANCE_LOW + notificação persistente), `notifications/NotificationHelper.kt` (`contentIntent` → `MainActivity`), `data/repository/DashboardRepository.kt` (reconnect loop, `lastEmittedSaleId`, venda perdida no reconnect, logging `DashboardRepo`).
- **Sintoma:** notificação de venda não chegava quando o app ficava em background — o WS caía (ex.: 13:14:04) e nunca reconectava; processo ficava vivo mas congelado (MIUI) sem CPU para reconectar. Além disso, o loop de reconexão original criava um socket novo a cada 5s **sem esperar o `onOpen` nem fechar o anterior** → conexões vazadas acumulavam no servidor (chegou a 15).
- **Solução:**
  - **Reconnect loop reescrito:** espera até 10s pelo `onOpen` (`while (!wsActive && agora < deadline) delay(250)`), depois `while (wsActive) delay(1000)` segura até a queda, `socket.close(1000, "reconnect")` antes da próxima tentativa e `delay(5000)`. Nada de socket duplicado.
  - **`LiveUpdatesService`:** Service `START_STICKY`; `startForeground` com notificação persistente "Resumo de Vendas ativo" (canal `live_updates`, IMPORTANCE_LOW); `serviceScope.launch { app.repository.newSaleEvents.collect { NotificationHelper.showSaleNotification(...) } }`. Impede o MIUI de congelar o processo.
  - **Singleton:** repositório criado UMA vez no `DashboardApplication` (`by lazy`), compartilhado entre `MainActivity` e o service — antes cada Activity criava um repositório novo (WS duplicado).
  - **`NotificationHelper`:** `contentIntent` (PendingIntent → `MainActivity`, `FLAG_ACTIVITY_NEW_TASK|SINGLE_TOP`) — tocar na notificação abre o app.
  - **Venda perdida no reconnect (`handleInit`):** se `recent_sales[0]` for recente (< 3 min) e `saleId > lastEmittedSaleId` (tracking em memória), notifica — venda nunca passa despercebida após reconexão.
  - **Logging:** `Log.d/w/e` com TAG `DashboardRepo` em `onOpen`/`onClosed`/`onFailure` (com o Throwable) e nas tentativas de conexão — diagnóstico via `adb logcat -s DashboardRepo:V`.
- **Verificação (15/ago/2026):** servidor limpou as conexões vazadas e mantém **1** WS estável; conexão sem queda por > 7 min com a tela desligada (background); venda criada via MySQL com tela apagada → notificação `id = 2000 + sale_id` chegou; tap na notificação (shade expandida + toque) abriu `MainActivity` (validado por `dumpsys activity top`); `bash ./gradlew assembleDebug` OK.
- **Nota MIUI:** para notificações confiáveis, orientar o usuário a ativar **Ajustes → Apps → Resumo de Vendas → Bateria → "Sem restrições"** + **inicialização automática**. Sem isso o MIUI pode congelar até apps com foreground service.
- **Pendência:** commits ainda não feitos (fork `merge-staging` e inventory-service `main`).

### 27. Deprecation `html_entity_decode(null)` em Sales.php (introduzido no fix #25)
- **Arquivo:** `app/Controllers/Sales.php` linhas 233 e 1247 (fork, prod e teste)
- **Sintoma:** `WARNING [DEPRECATED] Passing null to parameter #1` no log CI4 toda vez que uma venda era editada sem novo pagamento (`payment_type_new` ausente → `getPost` retorna `null`).
- **Causa raiz:** o fix #25 envolveu `getPost(...)` em `html_entity_decode(...)`, mas o campo pode vir `null` (postSave de edição de venda) — PHP 8.3 deprecia passar `null` a parâmetro string.
- **Solução:** `html_entity_decode((string) ...)` nas 2 linhas.
- **Aplicado em:** 16/ago/2026. Commitado em `1a0ff2b58`.

### 28. Deprecation dynamic property `Item_quantity::$stock_status`
- **Arquivo:** `app/Models/Item_quantity.php` (fork, prod e teste)
- **Sintoma:** `WARNING [DEPRECATED] Creation of dynamic property App\Models\Item_quantity::$stock_status` a cada load do Home/dashboard.
- **Causa raiz:** a coluna `stock_status` (fix #3) não tinha propriedade declarada no model; o CI4 cria dynamic property ao hidratar o row (proibido no PHP 8.2+).
- **Solução:** `public ?int $stock_status = null;` declarado no model.
- **Aplicado em:** 16/ago/2026. Commitado em `1a0ff2b58`.

### 29. 404 recorrente `items/attributes/-1` (form de item novo)
- **Arquivos:** `app/Config/Routes.php` (GET+POST) — fork, prod e teste
- **Sintoma:** 13 incidentes no guardrail (404.log 13-15/ago, referers `/sales/editItem/6`, `/items`, `/sales/add`); bloco de atributos nunca carregava no form de item novo.
- **Causa raiz:** `app/Views/items/form.php:84` faz `$('#attributes').load('items/attributes/$item_info->item_id')` com `item_id = -1` (item novo); a rota `(:num)` não casa `-1` → 404. `getAttributes(-1)` é seguro (retorna lista vazia).
- **Solução:** rotas `items/attributes/(:num)` → `([0-9-]+)` (como estavam antes do snapshot de 14/ago).
- **Verificação:** `curl /items/attributes/-1` passou de 404 → 302 (rota casa).
- **Aplicado em:** 16/ago/2026. Commitado em `1a0ff2b58`.

### 30. JS quebrado no preview de imagem (`url.indexOf is not a function`) — imgPreview + jQuery 3
- **Arquivos:** `public/js/imgpreview.full.jquery.js` (fonte, fork) + bundle `public/resources/js/imgpreview-1db063409f.full.jquery.js` (prod e teste)
- **Sintoma:** 7 ocorrências no `js-errors.log` (10-13/ago): `Uncaught TypeError: url.indexOf is not a function` ao pairar o mouse sobre thumbnail em /items — preview não abria.
- **Causa raiz:** o plugin usa `.load(function(){...})` e `.unbind('load')` — API do jQuery 2 removida no jQuery 3 (`$(img).load(fn)` virou o AJAX load, que tenta `url.indexOf` no handler). O fix original `c8486b79b` (`.on('load')`/`.off('load')`) foi revertido pelo snapshot `67b6d162e`.
- **Solução:** reaplicado `.on('load', function(){` e `.off('load')` na fonte e no bundle (patch cirúrgico, `node --check` OK), `cp` para prod e teste.
- **Aplicado em:** 16/ago/2026. Commitado em `1a0ff2b58`.

### 31. Alertas de estoque sempre vazios no app (filtro `stock_type` invertido)
- **Arquivo:** inventory-service `app/services/ospos_client.py` (`fetch_stock_alerts` + `fetch_stock_alert_count`)
- **Sintoma:** o app Android nunca mostrava alerta de estoque (badge e lista sempre vazios), embora houvesse 491 itens em alerta.
- **Causa raiz:** OSPOS define `stock_type = 0` como "item com estoque" (`Item.php:747` `where('stock_type', '0') // Stocked items only`) e `1` = não-estocável. As queries usavam `i.stock_type = 1` → **invertido**, retornavam sempre 0. Confirmado por SQL: `stock_type=1` → 0 alertas; `stock_type=0` → 491.
- **Solução:** `i.stock_type = 0` nas 2 queries.
- **Verificação:** `systemctl restart inventory.service`; `/v1/dashboard/alert-count` → `{"count":491}`; `/v1/dashboard/alerts` lista itens reais (ex.: 6573 status IRREGULAR qty 0); summary hoje continua OK.
- **Aplicado em:** 16/ago/2026. Commitado em `fc1a5cc` (sync/catalog) e `c6bc0ce` (main).

 ## Git — Fork `merge-staging`
Commits recentes (branch `merge-staging` — **pushed** para `origin/merge-staging` em 31/jul/2026):
```
1a0ff2b58 fix: audit bugs - html_entity_decode(null), Item_quantity stock_status property, attributes(-1) route, imgPreview jQuery 3 (16/ago/2026, pushed)
b7b745584 fix: guardrail 404 returns HTML body via echo (not setBody) (16/ago/2026, pushed)
0f5b0894a fix: move troco display to top of checkout modal so change is visible without scrolling
61396c8a0 fix: accept . and , as decimal separator system-wide; fix checkout troco for sales >= R$ 1000
fdfa8fea9 fix: guardrail js-error reporting, dead submit button, item save logging
98b924828 fix: move FINALIZAR to modal-footer, body scrolls independently
ed7442a9d fix: also skip auto-finish for PIX, needs manual confirmation like cash
72b2e0bf6 fix: set Content-Type application/json in addDiversos
39867df87 fix: remove modal height constraint, let Bootstrap handle overflow naturally
69e263c1c fix: widen checkout modal to 520px, remove duplicate conflicting CSS
0dcab9bd4 fix: enlarge checkout modal, always focus FINALIZAR when paid, fix Database.php
53a8a1ae7 fix: item save (env-based DB config), payment troco visibility, compact table layout
d48e4de95 fix: add missing Office routes (getIndex, logout)
6ab2ac356 fix: rotate item image via temp file + rename (works on files owned by other users) (6/ago/2026)
7b67683b2 feat: rotate item image (cw/ccw) from items form to fix sideways photos (5/ago/2026, ainda não pushed)
78b6e2cec feat: slim qty input, remove Editar Venda column and description row from sales cart (5/ago/2026, ainda não pushed)
339f1bfda feat: larger product photo above barcode in sales cart; slim price/qty columns (5/ago/2026, ainda não pushed)
dda24c598 feat: product photo thumbnail in sales cart + modal image viewer
```
- **Remote `origin`** aponta para `https://github.com/ismaeldouglasdev/opensourcepos.git` **sem token** (o PAT antigo, já revogado, foi removido do config). Autenticação via `gh auth setup-git` (CLI `gh` logado como `ismaeldouglasdev`).

## Loja-online — Elshaday Utilidades (`loja-online/`)
- **Deploy:** Render (`loja-online-kmg8.onrender.com`) — React 19 + Vite 8 + Tailwind 4
- **Produção:** `http://localhost:5173/` (dev local via Vite proxy)
- **Nome:** "Elshaday Utilidades" (Layout.tsx, Checkout.tsx WhatsApp msg)
- **Páginas:** Home (grid + busca + filtros), Detalhe (foto + upload + WhatsApp), Checkout, Captura, Admin
- **Carrinho:** `localStorage` key `elshaday_utilidades_cart`

### Sync com Render (CRÍTICO)
O Render **não** conecta ao MySQL local. O `inventory-service` no Render roda `seed_render.py` que lê um `catalog.json` versionado no git (branch `sync/prod-data`). Para manter a loja sincronizada:

**Fluxo automático (cron a cada 30min):**
1. `sync_render_auto.sh` roda `sync_catalog.py` (lê `/v1/store/sync-total` do inventory-service local)
2. Copia `catalog.json` + fotos PNG para `data/sync/`
3. Commit + push para `sync/prod-data`
4. Render re-deploya automaticamente (seed_render.py recria o SQLite com dados atualizados)

**Scripts:**
| Script | Função |
|--------|--------|
| `scripts/sync_render_auto.sh` | Sync automático (cron, 30min) — catálogo + fotos → git → Render |
| `scripts/sync_catalog.py` | Gera `catalog.json` + thumbnails WebP (lê API local) |
| `scripts/seed_render.py` | Roda no Render no boot — popula SQLite a partir de `data/sync/catalog.json` |

**Cron local:**
```
*/5 * * * * curl -s -X POST 'http://localhost:8000/v1/store/sync?mode=delta'  # OSPOS → SQLite local
*/30 * * * * /bin/bash /home/ismael/inventory-service/scripts/sync_render_auto.sh  # SQLite local → Render
```

**Dados visíveis:** produtos com foto + estoque > 0 + não-deletado. No MySQL local: ~75 itens. No Render: ~75 (varia com estoque).

### Imagens
- Inventory-service: `POST /v1/store/products/{id}/image/link` + CORS para localhost:8080
- Imagens locais: `/home/ismael/inventory-service/data/images/` (product_{id}.jpg/png)
- Fotos no Render: `data/sync/photos/ospos-item-images/` (copiadas do `uploads/item_pics/` local)

## App Android — Resumo de Vendas (`ospos-dashboard-app/`)
Dashboard Android nativo (Kotlin + Jetpack Compose, Material 3) que espelha o dashboard do OSPOS para o dono: totais do dia (por pagamentos), resumo por forma de pagamento, últimos vendas, alertas de estoque e **notificação a cada nova venda** (WS em tempo real).

### Stack / estrutura
- **Path:** `/home/ismael/ospos-dashboard-app/`
- **UI:** Compose — `ui/MainActivity.kt`, `ui/DashboardScreen.kt`, `ui/theme/`
- **Dados:** `data/repository/DashboardRepository.kt` (REST + WS via OkHttp + Moshi), `data/api/OSPOSApiService.kt`, `data/model/`
- **Notificações:** `notifications/NotificationHelper.kt` (canal `novas_vendas`, id = `2000 + sale_id`)
- **Serviço:** `services/LiveUpdatesService.kt` (foreground, canal `live_updates`, id 1001)
- **Conexão:** `DashboardApplication.kt` → `AppConfig` (HOST `192.168.15.6`, BASE_URL `http://192.168.15.6:8000/`, WS `ws://192.168.15.6:8000/v1/dashboard/ws`)

### Pontos-chave de arquitetura
- **Singleton do repositório:** criado `by lazy` no `DashboardApplication`; `MainActivity` e `LiveUpdatesService` usam `(application as DashboardApplication).repository` — UM WS, sem duplicatas.
- **Reconnect loop (WS):** espera até 10s pelo `onOpen` (`while (!wsActive && agora < deadline) delay(250)`), depois `while (wsActive) delay(1000)` segura até a queda, `socket.close(1000, "reconnect")` antes da próxima tentativa e `delay(5000)`. Nada de socket duplicado.
- **`LiveUpdatesService`:** Service `START_STICKY`; `startForeground` com notificação persistente "Resumo de Vendas ativo" (canal `live_updates`, IMPORTANCE_LOW); `serviceScope.launch { app.repository.newSaleEvents.collect { NotificationHelper.showSaleNotification(...) } }`. Impede o MIUI de congelar o processo.
- **Singleton:** repositório criado UMA vez no `DashboardApplication` (`by lazy`), compartilhado entre `MainActivity` e o service — antes cada Activity criava um repositório novo (WS duplicado).
- **`NotificationHelper`:** `contentIntent` (PendingIntent → `MainActivity`, `FLAG_ACTIVITY_NEW_TASK|SINGLE_TOP`) — tocar na notificação abre o app.
- **Venda perdida no reconnect (`handleInit`):** se `recent_sales[0]` for recente (< 3 min) e `saleId > lastEmittedSaleId` (tracking em memória), notifica — venda nunca passa despercebida após reconexão.
- **Cache offline rolling:** `rollingPeriodLabel()` detecta 30d/60d/90d e usa chaves normalizadas (`cache_rolling_30d`). Fallback seguro: `loadGlobalDataOnly()` carrega alerts+recent sem tocar no summary. WS init não sobrescreve dados de períodos != "today".
- **`isConnected`** (indicador/banner offline) segue o estado do WS: `onOpen`→true; `onClosed`/`onFailure`→false.

### Build e deploy (PC → celular)
```bash
# gradlew tem shebang sh mas sintaxe bash — SEMPRE via bash
export JAVA_HOME=/home/ismael/jdk-17.0.11+9
export ANDROID_HOME=/home/ismael/android-sdk
export PATH=$JAVA_HOME/bin:$PATH
bash ./gradlew assembleDebug --no-daemon          # workdir: /home/ismael/ospos-dashboard-app
# APK: app/build/outputs/apk/debug/app-debug.apk (~11,7MB)

# Celular (Xiaomi 14 Pro, 192.168.15.97). Porta adb que funciona:
adb connect 192.168.15.97:44507                   # porta de pareamento usada p/ `adb pair`
adb install -r app/build/outputs/apk/debug/app-debug.apk
adb shell pm grant com.ospos.dashboard android.permission.POST_NOTIFICATIONS
adb shell am start -n com.ospos.dashboard/.ui.MainActivity
adb logcat -s DashboardRepo:V                     # logs do WS (onOpen/onClosed/onFailure)
```

### Notificações
- Cada venda concluída gera notificação `id = 2000 + sale_id`, canal `novas_vendas` (IMPORTANCE_HIGH), `contentIntent` → `MainActivity`.
- Notificação persistente do foreground service (id 1001, canal `live_updates`, IMPORTANCE_LOW, `Ongoing`).
- **MIUI:** orientar usuário a **Ajustes → Apps → Resumo de Vendas → Bateria → "Sem restrições"** + **inicialização automática** — sem isso o MIUI congela até foreground service.
- Teste ponta-a-ponta: criar venda via MySQL (OSPOS) com tela apagada → notificação chega; tap abre o app.

### Backend (inventory-service) — contrato consumido pelo app
- `GET /v1/dashboard/summary?period=today|yesterday|week|month|custom&start=&end=` → `{sales_total, transactions, items_sold, avg_ticket, top_items[], hourly_sales[24], daily_target, target_pct, pending_receivables, prev_sales_total, change_pct, payment_summary[]}`. **Totais por pagamentos** (padrão: `SUM(payment_amount - cash_refund)` com `payment_amount > 0` e `sale_status = 0`). **`daily_target` escalado pelo nº de dias do período** (today=×1, week=×7, month=×dias_decorridos, custom=×span).
- `payment_summary[]` = `{payment_type, count, total}` — backend faz `html.unescape(payment_type)`.
- `GET /v1/dashboard/sales/recent?period=&start=&end=&limit=N` → `{sale_id, sale_time, customer, items_count, total}` — **filtra por período** (antes retornava sempre as N mais recentes globais).
- `WS /v1/dashboard/ws` → eventos `init` (snapshot), `sale` (por venda), `summary` (KPIs hoje, com `payment_summary`), `stock_alert`.
- Regra de exibição no app: eventos `summary`/`init` SÓ aplicam em `_currentPeriod == "today"` (não sobrescrevem o mês/semana selecionado).

### Fechamento
- Estado atual (19/ago/2026): totais por pagamentos batem entre app, `/home` e `sales/manage`; notificações confiáveis em background; `discount_type` e `payment_type` corrigidos; cache offline rolling funciona para 30d/60d/90d; recent_sales filtrado por período; meta diária escalada corretamente. **4 repos sync no GitHub.**

## Auditoria 16/ago/2026 — achados operacionais corrigidos

### 32. App Android — notificações de alerta de estoque + seed do poller
- **Arquivos:** app `ospos-dashboard-app` (`ApiModels.kt` novo `StockAlertEvent`, `DashboardRepository.kt` flow `stockAlertEvents`, `NotificationHelper.kt` canal `alerts_estoque` + `showStockAlertNotification`, `LiveUpdatesService.kt` coleta, `MainActivity.kt` `ensureStockChannel`, `strings.xml`); inventory-service `app/api/v1/dashboard.py` (seed da baseline de alertas no `_dashboard_poller`).
- **Funcionalidade:** cada item que entra em ZERADO/IRREGULAR gera notificação no celular (id `3000 + item_id`, canal `alerts_estoque` IMPORTANCE_HIGH). Eventos `cleared` só limpam o badge (sem notificação). Peso leva em conta o `stock_status`.
- **Seed do poller:** sem ele, a cada restart do inventory-service o poller (`last_alerts={}`) broadcastava TODOS os alertas existentes de uma vez → tempestade de ~492 notificações no app. Agora semeia `fetch_stock_alerts(50)` no boot; só mudanças reais são broadcast.
- **Verificação:** `py_compile` OK; restart + `/v1/dashboard/alert-count` → 492. App: `bash ./gradlew assembleDebug` BUILD SUCCESSFUL (11,7MB).
- **Instalação pendente:** celular com wireless debugging desligado (porta adb fechada) — re-parear e `adb install -r`. Commitado em `67061e4` (sync/catalog) + `bbd2f03` (main).

### 33. Full sync horário removido do cron (regra "só delta")
- O cron tinha `17 * * * * POST /v1/store/sync?mode=full` (varre 8.6k produtos a cada hora) além do delta a cada 5min — redundante e pesado, contradizia a regra do AGENTS ("só delta/incremental").
- **Solução:** linha removida do crontab; mantido `*/5 * * * * mode=delta`. O full é usado só manualmente/sob demanda.

### 34. Backups horários sem retenção (~40GB acumulados)
- `/home/ismael/pos-backups/backup.sh` (cron de hora em hora) criava `auto_*` com cópia do código + mysqldump + writable **sem nunca apagar** — 1591 dirs (~25MB cada) desde mar/2026, ~40GB.
- **Solução:** (1) limpeza manual em background apagando `auto_*` com mtime > 7 dias (disco 71% → 52%, ~18GB liberados até o momento); (2) adicionada retenção ao script: `find "$BACKUP_DIR" -maxdepth 1 -type d -name "auto_*" -mtime +7 -exec rm -rf -- {} +`.
- **Nota:** existem 3 mecanismos de backup sobrepostos — `pos-backups/backup.sh` (hora, 7d), `backup_ospos.sh` (15min, uncompressed 5MB, 7d) e `backup.sh` (3am, gzip, 30d). Candidatos a consolidação futura.

### 35. Dados corrigidos (MySQL prod)
- **Estoque negativo zerado:** `UPDATE ospos_item_quantities SET quantity=0 WHERE quantity<0` — 1477 itens (-4180 unid), resultado de vendas quando o estoque inicial não estava marcado. Prevenção já existia no código (`Sale::save` e `Item_quantity::change_quantity` clampeiam a 0 e marcam IRREGULAR/ZERADO) — verificado em fork/prod/teste.
- **7 vendas suspensas canceladas** (49, 99, 3945, 4701, 4788, 4797, 4975; ~R$235 pendentes): `UPDATE ospos_sales SET sale_status=2` (CANCELED). Vendas suspensas não tocam estoque (`Sale::delete` só restaura inventário p/ COMPLETED), então cancelar é seguro. Confirmado 0 suspensas restantes.
- **3 vendas de 22/mar sem pagamento** (ids 48, 50, 56 — primeiro dia, teste) mantidas; avaliar se devem ser canceladas.

### 36. Botão "Enviar" no modal de edição de itens travava — investigação completa (17/ago/2026)
- **Sintoma:** ao editar qualquer item (ex.: "CHAVEIRO CORDAO" id 2496) e clicar "Enviar", nada acontecia — modal ficava aberto, sem mensagem de erro, sem log de erro.
- **Causas-raiz encontradas (6 ao todo, combinavam para produzir o sintoma):**

#### Causa 1: Scripts inline não executavam no modal (JS)
- **Arquivo:** `public/js/manage_tables.js` — função `init()` (linha 86-93)
- **Causa:** `node.html(data)` injeta HTML mas jQuery 3 NÃO executa `<script>` tags via `.html()`. O `items/form.php` define `init_validation()` em `<script>` inline — nunca rodava → jQuery Validate nunca era anexado → `form.data('validator')` retornava `undefined`.
- **Solução:** após `node.html(data)`, percorrer scripts com `$.globalEval(this.textContent || this.text)` para executar inline scripts manualmente.

#### Causa 2: `checkNumeric` e `checkItemNumber` retornavam HTML do debugbar (PHP)
- **Arquivos:** `app/Controllers/Items.php` — `postCheckItemNumber()`, `postCheckNumeric()`
- **Causa:** `CI_ENVIRONMENT = development` no `.env` ativa o CodeIgniter Debugbar. Endpoints que fazem `echo 'true'` ou `echo 'false'` sem `setContentType('application/json')` tinham o HTML do debugbar anexado à resposta → jQuery parseava HTML como JSON → falha silenciosa.
- **Solução:** adicionar `$this->response->setContentType('application/json')` antes do `echo` em `postCheckNumeric()`, `postCheckItemNumber()`, `check_kit_exists()` e outros endpoints remotos.

#### Causa 3: Race condition no `submit()` (JS)
- **Arquivo:** `public/js/manage_tables.js` — função `submit()` (linha 13-29)
- **Causa:** o código original chamava `validator.valid()` DEPOIS de `form.submit()`. O `form.submit()` já dispara o jQuery Validate (que faz chamadas AJAX assíncronas para remote validators). A chamada `validator.valid()` duplicava essas chamadas AJAX, confundindo o pending count do jQuery Validate — o `submitHandler` nunca era chamado quando todos os remote validators retornavam.
- **Solução:** remover a chamada `validator.valid()` duplicada; resetar apenas `validator.formSubmitted = false` antes de `form.submit()`.

#### Causa 4: `item_number` era string literal `'NULL'` em 2928 itens (dados)
- **Tabela:** `ospos_items` — coluna `item_number`
- **Causa:** 2928 tinham `item_number = 'NULL'` (string de 4 bytes, HEX `4E554C4C`) em vez de SQL NULL. O `checkItemNumber()` envia `item_number=NULL` do formulário → `item_number_exists('NULL')` → `WHERE item_number = 'NULL' AND item_id != 2496` → encontrava TODOS os outros 2927 itens → jQuery Validate rejeitava com "O número do item já está presente na base de dados". O submitHandler nunca era chamado → botão parecia "morto".
- **Solução:** `UPDATE ospos_items SET item_number = NULL WHERE item_number = 'NULL'` (2928 linhas afetadas).
- **Nota:** os fixes JS (causas 1-3) tornaram o bug VISÍVEL — antes, o jQuery Validate nem rodava e o form submetia direto sem validação (que também falhava silenciosamente).

#### Causa 5: `Items::postSave()` sem Content-Type (PHP)
- **Arquivo:** `app/Controllers/Items.php:653`
- **Causa:** mesmo problema da causa 2, mas no endpoint de save. O debugbar HTML anexado ao JSON de resposta impedia o `ajaxSubmit` de parsear a resposta → `success` callback nunca rodava.
- **Solução:** adicionar `$this->response->setContentType('application/json')` antes do `echo json_encode(...)`.

#### Causa 6: Erros de servidor bloqueavam o save (PHP)
- **Arquivos:** `app/Controllers/Items.php` (`postSave`), `app/Models/Item_quantity.php`
- **Erros:** (a) `tax_names[]` vazios geravam entrada duplicada em `ospos_taxes_items` (PK: `item_id + tax_name + tax_percent`); (b) `description` null em coluna NOT NULL; (c) subtração de strings (`$updated_quantity - $item_quantity->quantity`); (d) `Item_quantity::$stock_status` tipada como `?int` mas CI4 hidrata como string.
- **Solução:** (a) filtro `strlen($tax_name) > 0` antes de `save_value()`; (b) `?? ''` em `getPost('description')`; (c) cast `(int)`; (d) property sem type hint.

- **Verificação (prod, 17/ago/2026):** `POST /items/save/2496` com unit_price=10.00 → `{"success":true,"message":"..."}`. Botão "Enviar" funciona, modal fecha, tabela atualiza.
- **Aplicado em:** 17/ago/2026. Produção e teste (prod e `/var/www/html/pos-test/`). Fork pendente.
- **Commits pendentes:** fonte `public/js/manage_tables.js` (causas 1+3), `app/Controllers/Items.php` (causas 2+5+6), `app/Models/Item_quantity.php` (causa 6d), dados MySQL (causa 4).

### 37. App Android — "Sem conexão" falso + reconnect lento + sem cache offline por período
- **Arquivos:** `ospos-dashboard-app/` — `data/repository/DashboardRepository.kt`, `ui/DashboardScreen.kt`, `ui/viewmodel/DashboardViewModel.kt`, `res/values/strings.xml`
- **Sintoma A:** banner "Sem conexão" aparecia mesmo quando a REST API funcionava — `isConnected` dependia **apenas** do WebSocket (`onOpen`→true, `onClosed`→false). Se o WS caía por 1 segundo, o app mostrava "Sem conexão" mesmo que o REST respondesse normalmente.
- **Sintoma B:** reconnect do WS era lento (delay fixo de 5s entre tentativas, sem backoff).
- **Sintoma C:** ao selecionar um período offline (ex.: "Personalizado"), o app mantinha os dados do período anterior em vez de carregar o cache do período selecionado. `loadPeriodCache()` existia mas nunca era chamado.
- **Solução:**
  1. **`connectionType`** StateFlow novo: `"realtime"` (WS ativo) / `"polling"` (WS down, REST respondeu nos últimos 60s) / `"offline"` (ambos falharam). `isConnected` = `wsActive || restAlive`.
  2. **Health check REST** — coroutine periódica faz `GET /v1/dashboard/summary` a cada 30s quando o WS está down, atualizando `restLastSuccess`. Se a REST responder, `isConnected` volta a true.
  3. **Exponential backoff** no WS reconnect: 2s → 4s → 8s → 16s → 30s max (era fixo 5s). Backoff reseta após conexão bem-sucedida.
  4. **Per-period cache** — `saveCache()` grava sob chave `cache_{period}_{start}_{end}`; no catch do `refresh()`, chama `loadPeriodCache(period, start, end)` → fallback para `loadCache()` se não houver cache do período.
  5. **Indicator visual** — dot no TopAppBar: verde (realtime), âmbar (polling), vermelho (offline). BottomStatusBar mostra "Tempo real" / "Polling" / "Offline" com a cor correspondente.
- **Verificação (17/ago/2026):** WS conecta em <1s (`onOpen` → "WS connected"). REST health check OK. App instalado via `adb install -r`.

### 38. App Android — UI reestruturada em abas (Resumo / Vendas / Estoque)
- **Arquivos:** `ospos-dashboard-app/` — `ui/DashboardScreen.kt` (reescrito), `res/values/strings.xml`
- **Sintoma:** a tela tinhaverticalmente demais — KPIs + pagamentos + vendas + gráfico + top itens + alertas, tudo em um LazyColumn. Usuário precisava scrollar muito.
- **Solução:** `NavigationBar` com 3 abas:
  - **Resumo** — KPIs (Faturamento, Vendas, Itens, Alertas) + resumo por pagamento + últimas 5 vendas (compacto) + botão "Ver todas as vendas →".
  - **Vendas** — Lista completa de vendas recentes + gráfico por hora + top mais vendidos.
  - **Estoque** — Cards de alertas de estoque com status ZERADO/IRREGULAR, badge com contagem no ícone da tab (vermelho quando > 0).
  - Ícones: `Icons.Outlined.Home`/`Filled.Home` (Resumo), `ShoppingCart` (Vendas), `Warning` (Estoque) — da core lib (sem dependency extra).

### 39. App Android — relatório com exportação (texto WhatsApp + PDF)
- **Arquivos:** `ospos-dashboard-app/` — `utils/ReportGenerator.kt` (novo), `res/xml/file_paths.xml` (novo), `AndroidManifest.xml` (FileProvider), `ui/DashboardScreen.kt` (FAB + dialog), `res/values/strings.xml`
- **Funcionalidade:** botão 📤 (FAB) na aba Resumo abre dialog "Compartilhar relatório" com:
  - **Texto (WhatsApp)** — `ReportGenerator.generateTextReport()` gera texto formatado com emojis: período, KPIs, pagamentos, top itens, vendas recentes (máx 10), alertas de estoque (zerados + irregulares). `shareAsText()` abre `Intent.ACTION_SEND` com `text/plain`.
  - **PDF** — `shareAsPdf()` gera PDF A4 via `PdfDocument` + `StaticLayout` (título, body mono), salva em `cacheDir`, compartilha via `FileProvider` (`cache-path` em `res/xml/file_paths.xml`, registrado no `AndroidManifest.xml` com `${applicationId}.fileprovider`).
- **Verificação:** build OK; dialog abre; share intent dispara corretamente.

### 40. Backend — "Esta semana" mostra apenas dados de hoje (periodo rolling)
- **Arquivo:** inventory-service `app/services/ospos_client.py` — `_build_date_filter()` (linha 463) + `_build_prev_date_filter()` (linha 491)
- **Sintoma:** "Esta semana" mostrava apenas dados de hoje (16 vendas) em vez dos 7 dias (~214 vendas).
- **Causa raiz:** o filtro usava `YEARWEEK(s.sale_time, 1) = YEARWEEK(CURDATE(), 1)` (calendário Mon-Dom). Hoje é segunda-feira (17/ago/2026) — a semana acabou de começar, então só havia dados de hoje.
- **Solução:** mudado para período rolling de 7 dias:
  - Current: `DATE(s.sale_time) >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)`
  - Previous: `DATE(s.sale_time) BETWEEN DATE_SUB(CURDATE(), INTERVAL 13 DAY) AND DATE_SUB(CURDATE(), INTERVAL 7 DAY)`
  - Label no app atualizado de "Esta semana" para "Últimos 7 dias".
  - `periodRangeText()` atualizado para mostrar "dd/MM/yyyy - dd/MM/yyyy" (últimos 6 dias a partir de hoje).
- **Verificação (17/ago/2026):** `GET /v1/dashboard/summary?period=week` → `sales_total=R$5421.90, transactions=214` (antes: 16). `inventory.service` reiniciado.

### 41. App Android — cache offline não funcionava para 30d/60d/90d (CRÍTICO)
- **Arquivos:** `ospos-dashboard-app/` — `data/repository/DashboardRepository.kt`
- **Sintoma:** ao desligar WiFi e selecionar "Últimos 30/60/90 dias", o app mostrava dados de "Hoje" com o label do período longo. O cache offline era inútil para períodos customizados.
- **Causa raiz:** a chave do cache incluía as datas exatas (`cache_custom_2026-07-21_2026-08-19`). Quando o usuário selecionava "30d" num dia diferente, as datas mudavam e o cache antigo era inutilizável. Além disso, o fallback (`loadCache()`) carregava o cache genérico (que era de "Hoje"), misturando dados de períodos distintos.
- **Solução (3 partes):**
  1. **Chaves normalizadas** — `rollingPeriodLabel(start, end)` detecta se o intervalo corresponde a 30/60/90 dias (diff exato entre start e end). Se sim, usa `cache_rolling_30d` em vez de `cache_custom_{start}_{end}`. Funciona cross-dia: 30d ontem e 30d hoje usam a mesma chave.
  2. **Fallback seguro** — `loadGlobalDataOnly()` carrega apenas alerts + recent sales (globais) do cache genérico, sem tocar no summary. Summary fica `null` → UI mostra "Sem dados offline para este período" em vez de dados errados.
  3. **WS init não sobrescreve** — `handleInit()` agora só atualiza `_recentSales` quando o período é "today" (antes, o WS sempre sobrescrevia com dados de hoje).
- **Verificação (19/ago/2026):** online → "30d" → desligar WiFi → "30d" → dados corretos do cache; "60d" sem cache → "Sem dados offline"; `adb install -r` OK.

### 42. App Android — "Meta diária" sem sentido para períodos longos
- **Arquivo:** `ospos-dashboard-app/` — `ui/DashboardScreen.kt` (TabResumo, KpiCard Faturamento)
- **Sintoma:** para "Últimos 30 dias", o card de Faturamento mostrava "Meta 99% · R$30.000" — a barra de progresso comparava o total de 30 dias com a meta DIÁRIA (R$1.000), Resultado: 2970% (ou 100% com clamp).
- **Causa raiz:** `targetPct` usava `daily_target` sem escalar pelo número de dias do período. Para 30d com meta R$1.000/dia, o esperado seria R$30.000 — mas o app comparava com R$1.000.
- **Solução (2 frentes):**
  1. **App** — barra de progresso e subtexto da meta escondidos quando `currentPeriod != "today"`. Evita confusão visual.
  2. **Backend** (`ospos_client.py`) — `daily_target` agora é multiplicado pelo número de dias do período (today=×1, week=×7, month=×dias_decorridos, custom=×span). `target_pct` calculado corretamente.
- **Verificação (19/ago/2026):** `GET /v1/dashboard/summary?period=custom&start=2026-07-21&end=2026-08-19` → `daily_target=30000, target_pct=99` (R$29.703 / R$30.000). `period=today` → `daily_target=1000, target_pct=31`.

### 43. App Android — vendas recentes ignoravam o período selecionado
- **Arquivos:** inventory-service `app/api/v1/dashboard.py` + `app/services/ospos_client.py`; `ospos-dashboard-app/` — `data/api/OSPOSApiService.kt` + `data/repository/DashboardRepository.kt`
- **Sintoma:** ao selecionar "Últimos 90 dias", a aba "Vendas" mostrava as mesmas 50 vendas mais recentes (de hoje), independentemente do período filtrado.
- **Causa raiz:** `GET /v1/dashboard/sales/recent` não aceitava filtros de data — sempre retornava as últimas N vendas globais. O app chamava `getRecentSales(50)` sem passar período.
- **Solução:**
  1. **Backend** — endpoint `/v1/dashboard/sales/recent` agora aceita `period`, `start`, `end` (mesmos parâmetros do summary). `fetch_recent_sales()` usa `_build_date_filter()` para filtrar por `sale_time`.
  2. **App** — `getRecentSales()` agora envia `period`/`start`/`end`. `refresh()` passa o período atual ao buscar vendas recentes.
- **Verificação (19/ago/2026):** `GET /v1/dashboard/sales/recent?limit=3&period=custom&start=2026-08-18&end=2026-08-19` → retorna só vendas de 18-19/ago. `period=today` → retorna só vendas de hoje.

### 44. Push completo — 4 repos sincronizados no GitHub
- **Repos:** `inventory-service` (sync/catalog), `loja-online` (main), `ospos-dashboard-app` (main, novo)
- **inventory-service:** merge de `sync/prod-data` (10 commits do outro PC: renome Elshaday, fix admin, inpaint key via env) + 2 commits novos (cache fixes backend). Conflito em `app/main.py` resolvido (dashboard + swarm + observability routers + SPA fallback).
- **loja-online:** 1 commit pushado.
- **ospos-dashboard-app:** git init + .gitignore (exclui .gradle, build/, *.apk, *.keystore, local.properties) + commit inicial (42 arquivos, 3375 linhas) + fix offline cache (3 arquivos, 60 linhas).
- **Aplicado em:** 19/ago/2026.

