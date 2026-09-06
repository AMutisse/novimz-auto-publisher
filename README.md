# Novimz Auto-Publisher (Python + GitHub Actions)

Substitui o Make.com por um script Python gratuito, agendado pelo GitHub
Actions, que replica exactamente o mesmo pipeline já validado em produção:

```
RSS -> filtro (recente + título não vazio) -> deduplicação
    -> Google Gemini AI (reescreve o artigo em JSON)
    -> Cloudflare Workers AI / FLUX.1-schnell (gera a imagem de capa)
    -> WordPress (upload da imagem + criação do post)
```

Custo: **0 USD/mês**, dentro dos limites gratuitos de cada serviço:
- GitHub Actions: gratuito para repositórios públicos; repositórios privados
  têm 2.000 minutos/mês grátis (este workflow demora tipicamente 1-3 minutos
  por execução, 8 execuções/dia = muito abaixo do limite).
- Google Gemini API: camada gratuita (verifica os limites actuais em
  https://ai.google.dev/pricing antes de escalar para muitos sites/artigos).
- Cloudflare Workers AI: camada gratuita (10.000 "Neurons"/dia, verifica em
  https://developers.cloudflare.com/workers-ai/platform/pricing/).
- WordPress: usa a tua instalação existente, sem custo adicional.

## 1. Criar o repositório no GitHub

1. Cria um repositório novo no GitHub (pode ser privado).
2. Copia todos os ficheiros desta pasta para esse repositório e faz o commit inicial.

## 2. Criar os "Repository secrets"

No GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
Cria estes 5 secrets (os valores nunca devem ser partilhados comigo nem
colados em nenhum chat — são só teus):

| Nome do secret              | Onde obter                                                                 |
|------------------------------|-----------------------------------------------------------------------------|
| `GEMINI_API_KEY`             | https://aistudio.google.com/apikey (a mesma conta/chave já usada no Make)  |
| `CLOUDFLARE_ACCOUNT_ID`      | Painel Cloudflare → o "Account ID" da tua conta                            |
| `CLOUDFLARE_API_TOKEN`       | Painel Cloudflare → My Profile → API Tokens → template "Workers AI"        |
| `NOVIMZ_WP_USER`             | O teu utilizador de administrador do WordPress (novimz.com)                |
| `NOVIMZ_WP_APP_PASSWORD`     | WordPress → Utilizadores → Perfil → "Application Passwords" (cria uma nova)|

Nota: podes reaproveitar a mesma Application Password "Automacao API 3h" que
já tinha sido criada no WordPress numa sessão anterior (nunca chegou a ser
usada), ou criar uma nova dedicada a este script.

## 3. Activar o agendamento

O workflow em `.github/workflows/publish.yml` já vem configurado para correr
automaticamente a cada 3 horas (`cron: "0 */3 * * *"`), assim que fizeres
push destes ficheiros para o branch principal. Não precisas de fazer mais
nada — o GitHub Actions ativa o cron automaticamente.

Podes também correr manualmente a qualquer momento em **Actions → Novimz
Auto-Publisher → Run workflow**, útil para testar sem esperar pelo próximo
ciclo de 3h.

## 4. Testar antes de confiar 100%

Recomendo testar primeiro com `--dry-run` (gera o artigo e a imagem mas não
publica no WordPress) antes da primeira execução real:

- Localmente (se tiveres Python 3.10+ instalado): define as 5 variáveis de
  ambiente acima na tua sessão de terminal e corre `python main.py --dry-run`.
- No GitHub Actions: podes editar temporariamente o passo "Correr o
  publicador" em `publish.yml` para incluir `--dry-run`, correr uma vez
  manualmente, e depois remover o `--dry-run` antes de deixar o agendamento
  automático activo.

Confirma nos logs da execução (separador "Actions" no GitHub) que aparece
`RESUMO DA EXECUÇÃO: N publicado(s)...` sem erros inesperados.

## 5. Como adicionar mais fontes RSS ou mais sites

Tudo o que precisa de ser ajustado está em `config.yaml`, que tem comentários
a explicar cada bloco. Em resumo:

- **Mais uma fonte RSS para o novimz.com**: acrescenta um bloco em `fontes:`
  dentro do site `novimz`.
- **Um site WordPress novo e independente**: duplica o bloco `- id: novimz
  ...` inteiro, muda o `id`, o `wordpress_base_url`, o `categoria_map` (os
  IDs de categoria são específicos de cada instalação WordPress — confirma-os
  em `/wp-admin/edit-tags.php?taxonomy=category` desse site) e cria os 2
  secrets `{ID}_WP_USER` / `{ID}_WP_APP_PASSWORD` correspondentes no GitHub.

Não é preciso tocar em `main.py` para isto — o script lê tudo a partir do
`config.yaml` e dos secrets.

## 6. Deduplicação de notícias

O `state/published.json` guarda um histórico (últimos ~12 dias) dos títulos
já publicados por site. Antes de publicar um novo item, o script compara o
título com esse histórico (semelhança de texto, não só igualdade exacta) e
ignora-o se for muito parecido com algo já publicado recentemente — isto
resolve um problema que existia no Make (a mesma notícia reaparecer como
"nova" no RSS dias depois). O workflow do GitHub Actions faz commit
automático deste ficheiro a cada execução, por isso não apagues nem edites
`state/published.json` manualmente.

## 7. Alvo de publicações por categoria (novo, 2026-09-06)

O script já não decide artigo a artigo, fonte a fonte — em cada execução,
junta os itens elegíveis de TODAS as fontes RSS do site e tenta publicar até
`alvo_publicacoes_por_categoria` (por omissão: 2) artigos em cada uma das 7
categorias, parando assim que todas as categorias atingirem esse número ou
quando os candidatos do RSS se esgotarem. A categoria só é conhecida depois
de a Gemini reescrever o artigo, por isso um item pode ser gerado e depois
ignorado se calhar numa categoria que já tenha atingido o alvo nesta
execução — nesse caso fica disponível para uma tentativa numa execução
seguinte, se ainda estiver dentro da janela do RSS.

**Atenção à quota gratuita da Gemini API (20 pedidos/dia)**: com 7 categorias
× 2 = até 14 chamadas à Gemini por execução, e um agendamento a cada 2 horas
(12 execuções/dia), o consumo pode facilmente ultrapassar os 20 pedidos/dia
da camada gratuita. Ajusta `alvo_publicacoes_por_categoria` e/ou
`limite_seguranca_gemini_por_execucao` no `config.yaml` conforme a tua quota
real, ou considera associar uma conta de facturação à API Gemini (Tier 1 —
upgrade quase instantâneo, limite de gasto inicial de $250, continua muito
barato) se precisares mesmo deste volume de publicações.

## 8. Decisão sobre o Make.com

Este script é uma réplica fiel do que já corria no Make (mesmo prompt
Gemini, mesmo pedido à Cloudflare, mesma lógica de categorias e publicação).
Depois de confirmares algumas execuções bem-sucedidas aqui, podes desactivar
(pausar) os 4 cenários no Make.com para parar de acumular custos lá — não é
preciso cancelar a conta imediatamente, só desligar o agendamento de cada
cenário em "Active".
