# Bot Academia — reservas automáticas (v1.8)

Robô que reserva as aulas do Jaime e da Ana Elisa na Walton Family Whole Health & Fitness,
cria o evento no calendário "Academia" do iCloud (com convite para a Ana Elisa) e avisa no WhatsApp.

| Aula | Dia | Horário |
|---|---|---|
| Mindful Mobility | Segunda | 5:30 pm |
| LIFT | Quarta | 4:30 pm |
| LIFT (Evening) | Quinta | 5:15 pm |

É acionado a cada 5 minutos pelo **cron-job.org** (o agendamento do próprio GitHub atrasa horas) e olha as próximas 2 semanas. Para mudar aulas, edite `config.json`.

---

## Passo 1 — Criar o repositório

1. Em github.com, clique em **New repository**.
2. Nome: **Bot-Academia** · Visibilidade: **Public** · clique em **Create repository**.
3. Clique em **uploading an existing file** e arraste: `bot.py`, `config.json`, `requirements.txt`, `README.md`, `.gitignore`.
4. Clique em **Commit changes**.
5. Crie o arquivo do agendamento: **Add file → Create new file**, nome
   `.github/workflows/academia.yml`, cole o conteúdo do arquivo `academia.yml` e clique em **Commit changes**.

## Passo 2 — Senha de app da Apple (para o calendário)

1. Entre em **account.apple.com** → **Login e Segurança** → **Senhas de Apps**.
2. Crie uma com o nome **Bot-Academia** e copie (formato `xxxx-xxxx-xxxx-xxxx`).
   Ela **não é** a sua senha da Apple: só acessa o calendário e pode ser revogada a qualquer momento.

## Passo 3 — WhatsApp da Ana Elisa (CallMeBot)

No celular dela, abra **callmebot.com/blog/free-api-whatsapp-messages** no Chrome e veja o número
atual do robô (ele muda de tempos em tempos). Adicione esse número aos contatos, mande pelo WhatsApp a mensagem
`I allow callmebot to send me messages` e anote a **apikey** que chegar.

## Passo 4 — Cadastrar os Secrets

No repositório: **Settings → Secrets and variables → Actions → New repository secret**. Crie um por um:

| Nome | Valor |
|---|---|
| `CA_USERNAME` | seu usuário do portal da academia |
| `CA_PASSWORD` | sua senha do portal da academia |
| `ICLOUD_EMAIL` | seu Apple ID |
| `ICLOUD_APP_PASSWORD` | a senha de app do Passo 2 |
| `ANA_EMAIL` | e-mail da Ana Elisa para o convite |
| `WA_PHONE_JAIME` | seu número com código do país (ex.: +1479…) |
| `WA_APIKEY_JAIME` | sua apikey do CallMeBot |
| `WA_PHONE_ANA` | número da Ana Elisa |
| `WA_APIKEY_ANA` | apikey dela (Passo 3) |

## Passo 5 — Testes

1. **Actions** → **Bot Academia - reservas automaticas** → **Run workflow** com `dry_run = true`.
   Ele só verifica e mostra no registro o que faria, sem reservar nada.
2. Depois rode de novo com `dry_run = false`. Essa execução cria no calendário os eventos das aulas
   em que vocês já estão inscritos (ex.: LIFT de 28/10).

Depois disso ele roda sozinho a cada 15 minutos.

## Passo 6 — Atalho no iPhone / Apple Watch (opcional)

1. Em github.com → **Settings (do seu perfil) → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**.
   Repositório: só **Bot-Academia** · Permissão: **Actions: Read and write**. Copie o token.
2. No app **Atalhos**, crie um atalho **Reservar Academia** com a ação **Obter Conteúdo de URL**:
   - URL: `https://api.github.com/repos/SEU_USUARIO/Bot-Academia/actions/workflows/academia.yml/dispatches`
   - Método: **POST**
   - Cabeçalhos: `Authorization` = `Bearer SEU_TOKEN` e `Accept` = `application/vnd.github+json`
   - Corpo (JSON): `ref` = `main`
3. Ative **Mostrar no Apple Watch** nos detalhes do atalho.

## Lista de espera

Quando a aula está lotada e o site oferece **Waitlist**, o robô coloca Jaime e Ana Elisa na lista de espera
e avisa no WhatsApp. Também cria no calendário um evento "⏳ Lista de Espera — …". Se a academia promover vocês, esse evento é trocado pelo evento normal da aula.

## Aulas desmarcadas manualmente

O robô guarda em `estado.json` quem ele já viu inscrito em cada aula e data. Se você ou a Ana Elisa desmarcarem
uma aula pelo app/site, ele percebe que a pessoa saiu e **não remarca**. Se ninguém mais estiver inscrito naquela data,
o evento do calendário "Academia" é apagado. Para voltar a marcar uma aula desmarcada, é só se inscrever de novo pelo app.

## Pausas (viagens)

No `config.json`, a lista `"pausas"` define períodos sem reservas (datas inclusivas). Exemplo atual:
`{"inicio": "2026-12-09", "fim": "2027-01-03", "motivo": "viagem de fim de ano"}`.
Para uma nova viagem, acrescente outro item na lista.

## Passo 7 — Acionamento a cada 5 minutos (cron-job.org)

O agendamento gratuito do GitHub atrasa de 2 a 4 horas. Por isso o robô é acionado de fora:

1. Crie um token no GitHub: **Settings (perfil) → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
   Repositório: só **Bot-Academia** · Permissão **Actions: Read and write**.
2. Em **cron-job.org**, crie uma conta grátis e um **Create cronjob**:
   - URL: `https://api.github.com/repos/d6t29ggrmy-del/Bot-Academia/actions/workflows/academia.yml/dispatches`
   - Agenda: a cada 5 minutos
   - Avançado → Método **POST**; Cabeçalhos: `Authorization: Bearer SEU_TOKEN`, `Accept: application/vnd.github+json`,
     `Content-Type: application/json`; Corpo: `{"ref":"main","inputs":{"notify_errors":"false"}}`
   - A resposta esperada é **204**.
3. O agendamento do GitHub fica como reserva, 1 vez por hora.

## Segurança

- Os Secrets ficam criptografados no GitHub; ninguém consegue lê-los, nem em repositório público.
- O código nunca imprime senhas; nos registros elas aparecem como `***`.
- Em caso de dúvida: revogue a senha de app da Apple e troque a senha da academia.
