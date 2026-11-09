"""
Bot Academia — Walton Family Whole Health & Fitness (Club Automation)
v2.2 — atualiza também eventos achados pelo horário (sem o UID do robô)
v2.1 — quem cancelou sai do evento do calendário (a Ana deixa de ser convidada); cancelar segue com a rodada normal
v2.0 — função "Cancelar aulas": cancela inscrições no portal sob pedido (entrada "cancelar" do workflow)
v1.9 — calendário pela situação real de cada um (inscrito x lista de espera); aviso quando só um de vocês está inscrito
v1.8 — respeita aulas desmarcadas manualmente (não remarca) e apaga o evento do calendário
v1.7 — lista de espera também no calendário ("Lista de Espera" no título); corrige formato do telefone do WhatsApp

O que faz, a cada execução:
  1. Entra no portal com a conta do Jaime.
  2. Para cada aula configurada em config.json, abre a página da aula e olha
     as datas dentro do horizonte (horizon_days) no dia da semana e horário certos.
  3. Se a data está aberta ("Sign Up") ou já "Registered", abre a janela de
     inscrição e garante que todos os membros (Jaime e Ana Elisa) estejam inscritos.
  4. Para toda data em que os membros estão inscritos, garante que exista um
     evento no calendário "Academia" do iCloud, com a Ana Elisa convidada.
  5. Avisa no WhatsApp (CallMeBot) quando fizer uma reserva nova.

Variáveis de ambiente (GitHub Secrets):
  CA_USERNAME, CA_PASSWORD           login do portal
  ICLOUD_EMAIL, ICLOUD_APP_PASSWORD  Apple ID + senha de app (calendário)
  ANA_EMAIL                          e-mail da Ana Elisa para o convite
  WA_PHONE_JAIME, WA_APIKEY_JAIME    CallMeBot do Jaime
  WA_PHONE_ANA, WA_APIKEY_ANA        CallMeBot da Ana Elisa
Opcionais:
  DRY_RUN=true        só verifica e relata, não reserva nem cria eventos
  SKIP_CALENDAR=true  não mexe no calendário
  NOTIFY_ERRORS=true  envia erro pelo WhatsApp na hora (padrão nas execuções pelo botão Run workflow)
"""

import json
import os
import re
import sys
import time
import traceback
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CFG["timezone"])
BASE = CFG["base_url"]
DEBUG_DIR = ROOT / "debug"
DEBUG_DIR.mkdir(exist_ok=True)

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"
SKIP_CALENDAR = os.getenv("SKIP_CALENDAR", "false").lower() == "true"
NOTIFY_ERRORS = os.getenv("NOTIFY_ERRORS", "false").lower() == "true"

ESTADO_PATH = ROOT / "estado.json"


def carregar_estado():
    """estado.json guarda, por aula+data, quem o robô já viu inscrito/na espera ("vistos")
    e quem desmarcou manualmente ("desmarcados"), para nunca remarcar essas pessoas."""
    try:
        return json.loads(ESTADO_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def salvar_estado(estado, today):
    limite = (today - timedelta(days=7)).strftime("%Y%m%d")
    estado = {k: v for k, v in estado.items() if k.rsplit("-", 1)[-1] >= limite}
    ESTADO_PATH.write_text(json.dumps(estado, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


WEEKDAYS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- WhatsApp
def normalizar_telefone(phone):
    """Deixa só os dígitos e garante o formato internacional (+1 para números dos EUA com 10 dígitos)."""
    if not phone:
        return phone
    digitos = re.sub(r"\D", "", phone)
    if len(digitos) == 10:
        digitos = "1" + digitos
    return "+" + digitos


def whatsapp(text, only_jaime=False):
    targets = [("JAIME", os.getenv("WA_PHONE_JAIME"), os.getenv("WA_APIKEY_JAIME"))]
    if not only_jaime:
        targets.append(("ANA", os.getenv("WA_PHONE_ANA"), os.getenv("WA_APIKEY_ANA")))
    for who, phone, key in targets:
        phone = normalizar_telefone(phone)
        if not phone or not key:
            log(f"WhatsApp {who}: sem credenciais, pulando")
            continue
        url = ("https://api.callmebot.com/whatsapp.php?phone="
               + urllib.parse.quote(phone) + "&text=" + urllib.parse.quote(text)
               + "&apikey=" + urllib.parse.quote(key))
        try:
            r = requests.get(url, timeout=30)
            txt = re.sub(r"<[^>]+>", " ", r.text or "")
            txt = re.sub(r"\s+", " ", txt).strip()
            if key:
                txt = txt.replace(key, "***")
            log(f"WhatsApp {who}: HTTP {r.status_code} — {txt[:200]}")
        except Exception as e:  # noqa
            log(f"WhatsApp {who}: falhou ({e})")
        time.sleep(2)


# ---------------------------------------------------------------- helpers
def parse_row_date(text, today):
    """'Wednesday, October 28 04:30pm - 05:15pm ...' -> (date, '04:30pm', '05:15pm')"""
    m = re.search(r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
                  r"([A-Za-z]+)\s+(\d{1,2})\s+(\d{1,2}:\d{2}[ap]m)\s*-\s*(\d{1,2}:\d{2}[ap]m)", text)
    if not m:
        return None
    _, month, day, start, end = m.groups()
    mo, d = MONTHS[month], int(day)
    year = today.year
    candidate = date(year, mo, d)
    if candidate < today - timedelta(days=60):  # virada de ano
        candidate = date(year + 1, mo, d)
    return candidate, start.lower(), end.lower()


def em_pausa(d):
    """Retorna o motivo se a data cai em alguma pausa do config.json (datas inclusivas)."""
    for p in CFG.get("pausas", []):
        if date.fromisoformat(p["inicio"]) <= d <= date.fromisoformat(p["fim"]):
            return p.get("motivo", "pausa")
    return None


def to_dt(d, hhmm_ampm):
    t = datetime.strptime(hhmm_ampm, "%I:%M%p").time()
    return datetime.combine(d, t, tzinfo=TZ)


def fmt_pt(d, start):
    return f"{WEEKDAYS_PT[d.weekday()]} {d:%d/%m} {start}"


def shot(page, name):
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{datetime.now():%H%M%S}-{name}.png"), full_page=True)
    except Exception:
        pass


def dismiss_popups(page):
    for sel in ['text=SNOOZE', 'text=Snooze', '.ui-dialog-titlebar-close:visible',
                'button:has-text("Close"):visible', '[aria-label="Close"]:visible']:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                loc.click(timeout=2000)
                time.sleep(0.5)
        except Exception:
            pass


# ---------------------------------------------------------------- portal
class WindowNotOpened(Exception):
    pass


def login(page):
    log("Fazendo login…")
    page.goto(BASE + "/", wait_until="domcontentloaded")
    time.sleep(2)
    dismiss_popups(page)
    if page.locator("text=Log Out").first.is_visible(timeout=1500):
        log("Já logado")
        return
    page.wait_for_selector("#caSignInLoginForm #login", state="attached", timeout=20000)
    dismiss_popups(page)
    # preenche direto nos campos do formulário (funciona mesmo com aviso por cima)
    user = os.environ["CA_USERNAME"].strip()
    pwd = os.environ["CA_PASSWORD"].strip()
    # diagnóstico sem expor dados: só tamanhos e formato
    log(f"  diagnóstico: usuário com {len(user)} caracteres, contém @: {'@' in user}, "
        f"senha com {len(pwd)} caracteres, espaços no meio: {' ' in user or ' ' in pwd}")
    page.fill("#caSignInLoginForm #login", user, force=True)
    page.fill("#caSignInLoginForm #password", pwd, force=True)
    dismiss_popups(page)
    try:
        with page.expect_response(lambda r: "/login/login" in r.url, timeout=30000) as resp_info:
            try:
                page.click("#loginButton", timeout=10000)
            except Exception:
                page.evaluate("document.getElementById('loginButton').click()")
        resp = resp_info.value
        body = ""
        try:
            body = resp.text()
        except Exception:
            pass
        body = re.sub(r'"[^"]*(token|session|csrf|key|hash)[^"]*"\s*:\s*"[^"]*"', '"<oculto>":"<oculto>"', body, flags=re.I)
        log(f"  diagnóstico: resposta do login HTTP {resp.status} — {body[:300]!r}")
    except PWTimeout:
        log("  diagnóstico: o site não fez a chamada /login/login após o clique")
    try:
        page.wait_for_function(
            "() => /\\/member/.test(location.pathname) || /Log Out/.test(document.body.innerText)",
            timeout=40000)
    except PWTimeout:
        shot(page, "login-falhou")
        msg = page.evaluate("""() => [...document.querySelectorAll('.error, .alert, .message, [class*=error], [class*=Error]')]
            .filter(e => e.offsetParent !== null).map(e => e.innerText.trim()).filter(Boolean).join(' | ').slice(0, 300)""")
        log(f"URL após login: {page.url.split('?')[0]}")
        raise RuntimeError("Login não confirmou" + (f" — site disse: {msg}" if msg else " (sem mensagem de erro na tela)"))
    page.wait_for_load_state("domcontentloaded")
    dismiss_popups(page)
    log("Login OK")


def open_event(page, event_id):
    page.goto(f"{BASE}/calendar/event-info?id={event_id}", wait_until="domcontentloaded")
    page.wait_for_selector("button.register_button", timeout=30000)
    time.sleep(1)


def read_rows(page):
    return page.evaluate("""() => [...document.querySelectorAll('button.register_button')].map((b, i) => {
        const row = b.closest('.col-md-12') || b.parentElement;
        return {i, cls: b.className, label: b.innerText.trim(),
                text: (row ? row.innerText : '').replace(/\\s+/g, ' ')};
    })""")


def get_description(page):
    try:
        return page.evaluate("""() => {
            const el = [...document.querySelectorAll('div,p,span,td')].find(e =>
                e.children.length === 0 && e.offsetParent !== null &&
                e.innerText.trim().length > 40 && !/Registered|Open|Sign Up/.test(e.innerText) &&
                !e.closest('.register_button') && e.getBoundingClientRect().top < 900);
            return el ? el.innerText.trim() : '';
        }""") or ""
    except Exception:
        return ""


def ensure_members(page, button_index, label_for_log, waitlist=False, info=None):
    """Abre a janela da data e inscreve os membros que faltam (ou os coloca na lista de espera).
    info = {"vistos": [...], "desmarcados": [...]} do estado.json; é atualizado aqui.
    Quem já foi visto inscrito e agora não está = desmarcou manualmente: não é reinscrito.
    Retorna (novos, todos_ok, presentes, inscritos) — inscritos = quem tem vaga confirmada."""
    if info is None:
        info = {"vistos": [], "desmarcados": []}
    presentes, inscritos = [], []
    btn = page.locator("button.register_button").nth(button_index)
    opened = False
    for tentativa in range(3):  # a janela às vezes demora; tenta até 3 vezes
        try:
            btn.scroll_into_view_if_needed()
            btn.click()
            page.wait_for_selector("div.select-member:visible", timeout=20000)
            opened = True
            break
        except PWTimeout:
            log(f"  janela de inscrição não abriu (tentativa {tentativa + 1}/3)")
            page.keyboard.press("Escape")
            time.sleep(3)
    if not opened:
        raise WindowNotOpened(f"janela de inscrição não abriu em {label_for_log}")
    time.sleep(1)

    newly = []
    for name in CFG["members"]:
        item = page.locator("div.select-member:visible").filter(
            has_text=re.compile(rf"^\s*{re.escape(name)}\b")).first
        if item.count() == 0:
            raise RuntimeError(f"Perfil '{name}' não apareceu na janela de inscrição")
        cls = item.get_attribute("class") or ""
        if "registered" in cls.split():
            log(f"  {name}: já inscrito")
            presentes.append(name)
            inscritos.append(name)
            continue
        if "waitlisted" in cls.split():
            log(f"  {name}: já está na lista de espera")
            presentes.append(name)
            continue
        if name in info["desmarcados"]:
            log(f"  {name}: desmarcou manualmente — não reinscrever")
            continue
        if name in info["vistos"]:
            log(f"  {name}: estava inscrito e agora não está — desmarcação manual, não reinscrever")
            info["desmarcados"].append(name)
            continue
        if DRY_RUN:
            log(f"  {name}: seria {'colocado na lista de espera' if waitlist else 'inscrito'} (DRY_RUN)")
            newly.append(name)
            continue
        item.click()
        page.wait_for_load_state("networkidle")
        time.sleep(1.5)
        cls_after = item.get_attribute("class") or ""
        if "unselected" in cls_after.split() and not "waitlist-selected" in cls_after.split():
            item.click()  # tenta de novo uma vez
            page.wait_for_load_state("networkidle")
            time.sleep(1.5)
            cls_after = item.get_attribute("class") or ""
            if "unselected" in cls_after.split() and not "waitlist-selected" in cls_after.split():
                shot(page, "selecao-falhou")
                raise RuntimeError(f"Não consegui selecionar '{name}' em {label_for_log}")
        newly.append(name)

    if not newly:
        page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
        time.sleep(1)
        return [], True, presentes, inscritos

    if DRY_RUN:
        page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
        time.sleep(1)
        return newly, False, presentes + newly, inscritos + ([] if waitlist else newly)

    botao = r"^\s*Add to Waitlist\s*$" if waitlist else r"^\s*Register\s*$"
    confirmacao = "text=/added to the waitlist/i" if waitlist else "text=/registration is complete/i"
    page.locator("a.button-round:visible", has_text=re.compile(botao)).first.click()
    try:
        page.wait_for_selector(confirmacao, timeout=30000)
    except PWTimeout:
        shot(page, "confirmacao-ausente")
        raise RuntimeError(f"Sem confirmação de {'lista de espera' if waitlist else 'inscrição'} em {label_for_log}")
    try:
        page.locator("a:visible, button:visible", has_text=re.compile(r"^\s*OK\s*$")).first.click(timeout=5000)
    except Exception:
        pass
    time.sleep(1)
    return newly, True, presentes + newly, inscritos + ([] if waitlist else newly)


# ---------------------------------------------------------------- calendário
class Calendar:
    def __init__(self):
        import caldav
        self.caldav = caldav
        client = caldav.DAVClient(url="https://caldav.icloud.com",
                                  username=os.environ["ICLOUD_EMAIL"],
                                  password=os.environ["ICLOUD_APP_PASSWORD"])
        self.principal = client.principal()
        cals = [c for c in self.principal.calendars() if (c.name or "").strip() == CFG["calendar_name"]]
        if not cals:
            raise RuntimeError(f"Calendário '{CFG['calendar_name']}' não encontrado no iCloud")
        self.cal = cals[0]
        try:
            self.organizer = str(self.principal.get_vcal_address()).replace("MAILTO:", "mailto:")
        except Exception:
            self.organizer = "mailto:" + os.environ["ICLOUD_EMAIL"]
        log(f"Calendário '{CFG['calendar_name']}' conectado")

    def delete_uid(self, uid):
        try:
            self.cal.event_by_uid(uid).delete()
            return True
        except Exception:
            return False

    def summary(self, uid):
        """Título do evento com esse UID, ou None se não existir."""
        try:
            ev = self.cal.event_by_uid(uid)
        except Exception:
            return None
        data = re.sub(r"\r?\n[ \t]", "", ev.data or "")  # junta linhas dobradas do iCalendar
        m = re.search(r"^SUMMARY[^:]*:(.*)$", data, re.M | re.I)
        t = m.group(1).strip() if m else ""
        return t.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")

    def exists(self, uid, start, end, title, waitlist=False):
        try:
            self.cal.event_by_uid(uid)
            return True
        except Exception:
            pass
        try:
            found = self.cal.search(start=start - timedelta(minutes=1), end=end + timedelta(minutes=1),
                                    event=True, expand=False)
            for ev in found:
                data = ev.data or ""
                m = re.search(r"^SUMMARY[^:]*:(.*)$", data, re.M | re.I)
                summary = m.group(1) if m else ""
                eh_espera = "lista de espera" in summary.lower()
                if eh_espera != waitlist:
                    continue
                base_name = title.replace("⏳ Lista de Espera — ", "").split(" @ ")[0].split(" (")[0]
                if base_name.lower() in summary.lower():
                    return True
        except Exception as e:
            log(f"  busca no calendário falhou ({e}); vou criar mesmo assim")
        return False

    def find(self, uid, start, end, title, waitlist=False):
        """Acha o evento pelo UID do robô ou, se não houver, pelo horário + nome da aula."""
        try:
            return self.cal.event_by_uid(uid)
        except Exception:
            pass
        try:
            for ev in self.cal.search(start=start - timedelta(minutes=1), end=end + timedelta(minutes=1),
                                      event=True, expand=False):
                summary = self.titulo(ev).lower()
                if ("lista de espera" in summary) != waitlist:
                    continue
                base_name = title.replace("⏳ Lista de Espera — ", "").replace("⚠️ ", "").split(" @ ")[0].split(" (")[0]
                if base_name.lower() in summary:
                    return ev
        except Exception as e:
            log(f"  busca no calendário falhou ({e})")
        return None

    @staticmethod
    def _data(ev):
        return re.sub(r"\r?\n[ \t]", "", ev.data or "")

    def titulo(self, ev):
        m = re.search(r"^SUMMARY[^:]*:(.*)$", self._data(ev), re.M | re.I)
        t = m.group(1).strip() if m else ""
        return t.replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")

    def convida_ana(self, ev):
        # o iCloud reescreve o convidado (EMAIL=... e endereço interno), então olha nome e e-mail
        ana = os.getenv("ANA_EMAIL", "").strip().lower()
        for l in self._data(ev).splitlines():
            u = l.lower()
            if u.startswith("attendee") and ((ana and ana in u) or 'cn="ana elisa"' in u or "cn=ana elisa" in u):
                return True
        return False

    def tem_ana(self, uid):
        """True se o evento com esse UID tem a Ana Elisa como convidada (None se não existir)."""
        try:
            ev = self.cal.event_by_uid(uid)
        except Exception:
            return None
        ana = os.getenv("ANA_EMAIL", "").strip().lower()
        data = re.sub(r"\r?\n[ \t]", "", ev.data or "").lower()
        return bool(ana) and ("mailto:" + ana) in data

    def create(self, uid, start, end, title, description, convidar_ana=True):
        def esc(s):
            return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")
        utc = ZoneInfo("UTC")
        stamp = datetime.now(utc).strftime("%Y%m%dT%H%M%SZ")
        s = start.astimezone(utc).strftime("%Y%m%dT%H%M%SZ")
        e = end.astimezone(utc).strftime("%Y%m%dT%H%M%SZ")
        desc = CFG["phone"] + ("\n\n" + description if description else "")
        ana = os.getenv("ANA_EMAIL", "").strip()
        lines = [
            "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//academia-bot//PT", "CALSCALE:GREGORIAN",
            "BEGIN:VEVENT",
            f"UID:{uid}", f"DTSTAMP:{stamp}", f"DTSTART:{s}", f"DTEND:{e}",
            f"SUMMARY:{esc(title)}",
            f"LOCATION:{esc(CFG['location'])}",
            f"DESCRIPTION:{esc(desc)}",
            "X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC",
            f"ORGANIZER;CN=Jaime:{self.organizer}",
        ]
        if ana and convidar_ana:
            lines.append(f"ATTENDEE;CN=Ana Elisa;CUTYPE=INDIVIDUAL;ROLE=REQ-PARTICIPANT;"
                         f"PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{ana}")
        lines += [
            "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Lembrete",
            f"TRIGGER:-PT{CFG['alert_minutes_before']}M", "END:VALARM",
            "END:VEVENT", "END:VCALENDAR",
        ]
        self.cal.save_event("\r\n".join(lines))


# ---------------------------------------------------------------- diagnóstico (só leitura)
def diagnostico():
    """Abre a janela de uma aula em que vocês já estão inscritos e registra no log como ela é,
    sem clicar em nenhum nome nem confirmar nada (usado para preparar a função de cancelar)."""
    log("Diagnóstico — só leitura, nada será alterado")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1300, "height": 1000})
        page.set_default_timeout(30000)
        try:
            login(page)
            links = page.evaluate("""() => [...document.querySelectorAll('a')].map(a => (a.innerText.trim() + ' -> ' + a.getAttribute('href')))
                .filter(t => /reserv|schedule|my |calendar|class/i.test(t))""")
            log("Links: " + " | ".join(links[:40]))
            for texto in ["Reservations"]:
                try:
                    page.locator("a", has_text=re.compile(rf"^\s*{texto}\s*$")).first.click()
                    page.wait_for_load_state("networkidle")
                    time.sleep(3)
                    log(f"Página {texto}: {page.url}")
                    corpo = page.evaluate("() => document.body.innerText.replace(/\\s+/g, ' ')")
                    for k in range(0, min(len(corpo), 6000), 1500):
                        log("TEXTO " + corpo[k:k + 1500])
                    html = page.evaluate("() => (document.querySelector('#content, .content, main') || document.body).innerHTML.replace(/\\s+/g, ' ')")
                    for k in range(0, min(len(html), 9000), 1500):
                        log("HTML " + html[k:k + 1500])
                except Exception as e:
                    log(f"Falhou {texto}: {e}")
            return
            for c in CFG["classes"]:
                open_event(page, c["event_id"])
                rows = read_rows(page)
                alvo = next((r for r in rows if re.search(r"^\s*Registered\s*$", r["label"], re.I)), None)
                if not alvo:
                    continue
                log(f"Aula {c['name']}: linha '{alvo['text'][:80]}' cls='{alvo['cls']}'")
                btn = page.locator("button.register_button").nth(alvo["i"])
                btn.scroll_into_view_if_needed()
                btn.click()
                page.wait_for_selector("div.select-member:visible", timeout=20000)
                time.sleep(2)
                html = page.evaluate("""() => {
                    const m = document.querySelector('div.select-member');
                    let el = m;
                    for (let k = 0; k < 8 && el && el.parentElement; k++) {
                        el = el.parentElement;
                        if (el.matches('.modal, .ui-dialog, [role=dialog], .popup, .dialog')) break;
                    }
                    return (el || m).outerHTML.replace(/\\s+/g, ' ');
                }""")
                for k in range(0, min(len(html), 12000), 1500):
                    log("HTML " + html[k:k + 1500])
                vis = page.evaluate("""() => [...document.querySelectorAll('a, button')]
                    .filter(e => e.offsetParent !== null)
                    .map(e => e.tagName + '.' + e.className + ' [' + e.innerText.trim().slice(0, 40) + ']')
                    .filter(t => !/\\[\\]$/.test(t)).slice(-40)""")
                log("Botões visíveis: " + " | ".join(vis))
                shot(page, "diagnostico-janela")
                page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
                log("Janela fechada com Cancel, sem alterações")
                break
        finally:
            browser.close()


# ---------------------------------------------------------------- cancelar aulas (sob pedido)
def _abrir_janela(page, c, d):
    """Abre a janela de inscrição da aula c na data d. Retorna True se abriu."""
    open_event(page, c["event_id"])
    today = datetime.now(TZ).date()
    for r in read_rows(page):
        parsed = parse_row_date(r["text"], today)
        if not parsed or parsed[0] != d or parsed[1] != c["start"].lower():
            continue
        btn = page.locator("button.register_button").nth(r["i"])
        for _ in range(3):
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
                page.wait_for_selector("div.select-member:visible", timeout=20000)
                time.sleep(1)
                return True
            except PWTimeout:
                page.keyboard.press("Escape")
                time.sleep(3)
    return False


def _membro(page, name):
    return page.locator("div.select-member:visible").filter(
        has_text=re.compile(rf"^\s*{re.escape(name)}\b")).first


def _botoes_visiveis(page):
    return page.evaluate("""() => [...document.querySelectorAll('a, button, input[type=button], input[type=submit]')]
        .filter(e => e.offsetParent !== null)
        .map(e => e.tagName + '.' + e.className + ' [' + (e.innerText || e.value || '').trim().slice(0, 40) + ']')
        .filter(t => !/\\[\\]$/.test(t)).slice(-30)""")


def cancelar(pedidos):
    """pedidos = [{"event_id": 624023, "data": "2026-11-09", "membros": ["Jaime", "Ana Elisa"]}, ...]
    Cancela no portal, marca como desmarcado no estado.json (para não remarcar),
    ajusta o calendário e avisa no WhatsApp."""
    log(f"Cancelar aulas — pedidos: {json.dumps(pedidos, ensure_ascii=False)}")
    estado = carregar_estado()
    today = datetime.now(TZ).date()
    feitos, falhas, apagar = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1300, "height": 1000})
        page.set_default_timeout(30000)
        page.on("dialog", lambda dlg: (log(f"  confirmação do site: {dlg.message!r} → OK"), dlg.accept()))
        try:
            login(page)
            for ped in pedidos:
                c = next((x for x in CFG["classes"] if str(x["event_id"]) == str(ped["event_id"])), None)
                if not c:
                    falhas.append(f"aula {ped['event_id']} não está no config.json")
                    continue
                d = date.fromisoformat(ped["data"])
                label = f"{c['name']} {fmt_pt(d, c['start'].lower())}"
                chave = f"{c['event_id']}-{d:%Y%m%d}"
                info = estado.setdefault(chave, {"vistos": [], "desmarcados": []})
                for name in ped["membros"]:
                    if name not in info["desmarcados"]:
                        info["desmarcados"].append(name)  # mesmo se falhar, o robô não remarca
                cancelados = []
                for name in ped["membros"]:
                    if not _abrir_janela(page, c, d):
                        falhas.append(f"{label}: não consegui abrir a janela da aula")
                        shot(page, "cancelar-janela")
                        break
                    item = _membro(page, name)
                    cls = (item.get_attribute("class") or "").split() if item.count() else []
                    if "registered" not in cls and "waitlisted" not in cls:
                        log(f"  {label} — {name}: já não estava inscrito nem na espera")
                        cancelados.append(name)
                        page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
                        continue
                    link = item.locator("a.unregister-link, a", has_text=re.compile(
                        r"unregister|remove|leave|cancel", re.I)).first
                    if link.count() == 0:
                        falhas.append(f"{label} — {name}: não achei o link Unregister")
                        log("  HTML: " + item.evaluate("e => e.outerHTML")[:1500])
                        shot(page, "cancelar-sem-link")
                        continue
                    if DRY_RUN:
                        log(f"  {label} — {name}: seria cancelado (DRY_RUN)")
                        page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
                        cancelados.append(name)
                        continue
                    link.click()
                    page.wait_for_load_state("networkidle")
                    time.sleep(2)
                    log("  depois do Unregister, botões visíveis: " + " | ".join(_botoes_visiveis(page)))
                    # confirmação dentro da página (se houver)
                    conf = page.locator("a:visible, button:visible, input:visible", has_text=re.compile(
                        r"^\s*(Yes|OK|Confirm|Unregister|Continue)\b", re.I))
                    try:
                        for k in range(conf.count()):
                            el = conf.nth(k)
                            if "unregister-link" in (el.get_attribute("class") or ""):
                                continue
                            log(f"  clicando na confirmação: {el.inner_text().strip()[:30]}")
                            el.click()
                            page.wait_for_load_state("networkidle")
                            time.sleep(2)
                            break
                    except Exception as e:
                        log(f"  aviso na confirmação: {e}")
                    # verificação: reabre a janela e confere o nome
                    page.keyboard.press("Escape")
                    if _abrir_janela(page, c, d):
                        cls2 = (_membro(page, name).get_attribute("class") or "").split()
                        page.locator("a:visible", has_text=re.compile(r"^\s*Cancel\s*$")).first.click()
                        if "registered" in cls2 or "waitlisted" in cls2:
                            falhas.append(f"{label} — {name}: o site ainda mostra a inscrição")
                            shot(page, "cancelar-nao-confirmou")
                            continue
                    log(f"  {label} — {name}: cancelado ✔")
                    cancelados.append(name)
                if cancelados:
                    feitos.append((label, cancelados))
                restantes = [n for n in CFG["members"] if n not in info["desmarcados"]]
                if not restantes or set(cancelados) >= set(CFG["members"]):
                    apagar.append((c, d))
        except Exception as e:
            shot(page, "cancelar-erro")
            falhas.append(f"Erro geral: {e}")
            traceback.print_exc()
        finally:
            browser.close()
    if not DRY_RUN:
        salvar_estado(estado, today)
    if apagar and not DRY_RUN and not SKIP_CALENDAR:
        try:
            cal = Calendar()
            for c, d in apagar:
                for uid in (f"academia-{c['event_id']}-{d:%Y%m%d}@academia-bot",
                            f"academia-espera-{c['event_id']}-{d:%Y%m%d}@academia-bot"):
                    if cal.delete_uid(uid):
                        log(f"  calendário: removido {c['name']} {d:%d/%m}")
        except Exception as e:
            falhas.append(f"Calendário: {e}")
    linhas = ["❌ BOT ACADEMIA — aula cancelada" + (" (teste)" if DRY_RUN else "")]
    linhas += [f"🗓️ {label} — {' e '.join(nomes)}" for label, nomes in feitos]
    if falhas:
        linhas.append("⚠️ Não consegui: " + "; ".join(falhas[:4]))
    if (feitos or falhas) and not DRY_RUN:
        whatsapp("\n".join(linhas))
    log(f"Fim do cancelamento — feitos: {len(feitos)}, falhas: {len(falhas)}")
    if falhas:
        log("FALHAS:\n  " + "\n  ".join(falhas))
    return falhas


# ---------------------------------------------------------------- principal
def tipo_evento(presentes, inscritos):
    """Decide o evento do calendário pela situação de cada um (e não pela cor do botão da aula):
    ok = todos os presentes com vaga; parcial = um com vaga e outro na espera; espera = ninguém com vaga."""
    na_espera = [n for n in presentes if n not in inscritos]
    if not inscritos:
        return "espera", [], na_espera
    if na_espera:
        return "parcial", list(inscritos), na_espera
    return "ok", list(inscritos), []


def main():
    if os.getenv("TESTE_WHATSAPP", "false").lower() == "true":
        log("Teste de WhatsApp (só para o Jaime)")
        whatsapp("🧪 BOT ACADEMIA — teste de WhatsApp. Se você recebeu esta mensagem, o envio está funcionando.", only_jaime=True)
        return
    if os.getenv("DIAGNOSTICO", "false").lower() == "true":
        diagnostico()
        return
    falhas_cancelar = []
    if os.getenv("CANCELAR", "").strip():
        falhas_cancelar = cancelar(json.loads(os.environ["CANCELAR"]))
        log("Seguindo com a rodada normal para acertar o calendário")
    today = datetime.now(TZ).date()
    horizon = today + timedelta(days=CFG["horizon_days"])
    log(f"Início — DRY_RUN={DRY_RUN} — horizonte até {horizon:%d/%m/%Y}")

    booked, problems, to_calendar, waitlisted = [], [], [], []
    problems += [f"Cancelamento: {f}" for f in falhas_cancelar]
    estado = carregar_estado()
    to_remove = []  # (c, d, start) — todos desmarcaram: apagar evento do calendário

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1300, "height": 1000})
        page.set_default_timeout(30000)
        try:
            login(page)
            for c in CFG["classes"]:
                log(f"Aula: {c['name']} ({c['weekday']} {c['start']})")
                open_event(page, c["event_id"])
                description = get_description(page)
                rows = read_rows(page)
                targets = []
                for r in rows:
                    parsed = parse_row_date(r["text"], today)
                    if not parsed:
                        continue
                    d, start, end = parsed
                    if not r["text"].startswith(c["weekday"]) or start != c["start"].lower():
                        continue
                    if d < today or d > horizon:
                        continue
                    pausa = em_pausa(d)
                    if pausa:
                        log(f"  {fmt_pt(d, start)}: em pausa ({pausa}) — não reservar")
                        continue
                    targets.append((d, start, end, r))

                for d, start, end, r in targets:
                    label = f"{c['name']} {fmt_pt(d, start)}"
                    status = r["label"]
                    log(f"  {fmt_pt(d, start)}: {status}")
                    if to_dt(d, start) <= datetime.now(TZ):
                        continue
                    if "register-button-now" in r["cls"] or "register-button-registered" in r["cls"]:
                        open_event(page, c["event_id"])  # recarrega para evitar estado velho
                        rows_now = read_rows(page)
                        idx = next((x["i"] for x in rows_now if x["text"].startswith(r["text"][:40])), None)
                        if idx is None:
                            problems.append(f"{label}: linha sumiu da página")
                            continue
                        chave = f"{c['event_id']}-{d:%Y%m%d}"
                        info = estado.setdefault(chave, {"vistos": [], "desmarcados": []})
                        try:
                            newly, all_in, presentes, inscritos = ensure_members(page, idx, label, info=info)
                        except WindowNotOpened as e:
                            if "register-button-registered" in r["cls"]:
                                # já aparece como inscrito: não é problema, segue para o calendário
                                log(f"    aviso: {e} — mas a aula já consta como Registered")
                                to_calendar.append((c, d, start, end, description, "ok", [], []))
                            else:
                                problems.append(f"{label}: {e}")
                                shot(page, "erro-inscricao")
                            continue
                        except Exception as e:
                            problems.append(f"{label}: {e}")
                            shot(page, "erro-inscricao")
                            continue
                        if newly:
                            booked.append((label, newly))
                        if not DRY_RUN:
                            info["vistos"] = sorted(set(info["vistos"]) | set(presentes))
                            info["situacao"] = {n: ("inscrito" if n in inscritos else "espera") for n in presentes}
                        if not presentes:
                            if info["desmarcados"]:
                                to_remove.append((c, d, start))
                        elif all_in or DRY_RUN:
                            to_calendar.append((c, d, start, end, description, *tipo_evento(presentes, inscritos)))
                    elif "register-button-waitlist" in r["cls"] or re.search(r"waitlist", status, re.I):
                        open_event(page, c["event_id"])
                        rows_now = read_rows(page)
                        idx = next((x["i"] for x in rows_now if x["text"].startswith(r["text"][:40])), None)
                        if idx is None:
                            problems.append(f"{label}: linha sumiu da página")
                            continue
                        chave = f"{c['event_id']}-{d:%Y%m%d}"
                        info = estado.setdefault(chave, {"vistos": [], "desmarcados": []})
                        try:
                            newly, all_ok, presentes, inscritos = ensure_members(page, idx, label, waitlist=True, info=info)
                        except WindowNotOpened as e:
                            log(f"    aviso: {e} (lista de espera)")
                            if re.search(r"^\s*Registered\s*$", status, re.I):
                                to_calendar.append((c, d, start, end, description, "ok", [], []))
                            continue
                        except Exception as e:
                            problems.append(f"{label} (lista de espera): {e}")
                            shot(page, "erro-lista-espera")
                            continue
                        if newly:
                            waitlisted.append((label, newly))
                        if not DRY_RUN:
                            info["vistos"] = sorted(set(info["vistos"]) | set(presentes))
                            info["situacao"] = {n: ("inscrito" if n in inscritos else "espera") for n in presentes}
                        if not presentes:
                            if info["desmarcados"]:
                                to_remove.append((c, d, start))
                        elif all_ok or DRY_RUN:
                            to_calendar.append((c, d, start, end, description, *tipo_evento(presentes, inscritos)))
                    elif "full" in r["cls"].lower() or re.search(r"full", status, re.I):
                        log("    lotada, sem lista de espera — nada a fazer")
                    else:
                        log("    ainda não abriu")
        except Exception as e:
            shot(page, "erro-geral")
            problems.append(f"Erro geral: {e}")
            traceback.print_exc()
        finally:
            browser.close()

    if not DRY_RUN:
        salvar_estado(estado, today)

    # ----- calendário: aulas desmarcadas por todos → apagar eventos
    if to_remove and not SKIP_CALENDAR and not DRY_RUN:
        try:
            cal_rm = Calendar()
            for c, d, start in to_remove:
                for uid in (f"academia-{c['event_id']}-{d:%Y%m%d}@academia-bot",
                            f"academia-espera-{c['event_id']}-{d:%Y%m%d}@academia-bot"):
                    if cal_rm.delete_uid(uid):
                        log(f"  calendário: removido {c['name']} {fmt_pt(d, start)} (aula desmarcada)")
        except Exception as e:
            problems.append(f"Calendário (remoção): {e}")
            traceback.print_exc()

    # ----- calendário
    created, separados = [], []
    if to_calendar and not SKIP_CALENDAR:
        try:
            cal = None if DRY_RUN else Calendar()
            for c, d, start, end, description, tipo, com_vaga, na_espera in to_calendar:
                espera = tipo == "espera"
                if espera:
                    title = f"⏳ Lista de Espera — {c['name']} @ Walmart"
                elif tipo == "parcial":
                    title = (f"⚠️ {c['name']} @ Walmart — só {' e '.join(com_vaga)} inscrito"
                             f" ({' e '.join(na_espera)} na espera)")
                elif com_vaga == ["Ana Elisa"]:
                    title = f"{c['name']} @ Walmart — só Ana Elisa"
                else:
                    title = f"{c['name']} @ Walmart"
                # a Ana só é convidada se ela estiver na aula (inscrita ou na espera)
                convidar_ana = (not com_vaga and not na_espera) or "Ana Elisa" in (com_vaga + na_espera)
                s, e = to_dt(d, start), to_dt(d, end)
                uid_ok = f"academia-{c['event_id']}-{d:%Y%m%d}@academia-bot"
                uid_espera = f"academia-espera-{c['event_id']}-{d:%Y%m%d}@academia-bot"
                uid = uid_espera if espera else uid_ok
                if DRY_RUN:
                    log(f"  calendário (DRY_RUN): {title} {fmt_pt(d, start)}")
                    continue
                if not espera and cal.delete_uid(uid_espera):
                    log(f"  calendário: removido o evento de lista de espera de {c['name']} {fmt_pt(d, start)} (vaga confirmada)")
                if espera and cal.delete_uid(uid_ok):
                    log(f"  calendário: removido o evento confirmado de {c['name']} {fmt_pt(d, start)} (voltou para a lista de espera)")
                ev = cal.find(uid, s, e, title, waitlist=espera)
                if ev is not None:
                    if cal.titulo(ev) == title and cal.convida_ana(ev) == convidar_ana:
                        log(f"  calendário: já existe {title} {fmt_pt(d, start)}")
                        continue
                    ev.delete()  # título ou convidados mudaram: recria
                    log(f"  calendário: atualizando {c['name']} {fmt_pt(d, start)}"
                        + ("" if convidar_ana else " (sem a Ana Elisa)"))
                if tipo == "parcial":
                    separados.append(f"{c['name']} {fmt_pt(d, start)} — só {' e '.join(com_vaga)} inscrito; "
                                     f"{' e '.join(na_espera)} continua na lista de espera")
                cal.create(uid, s, e, title, description, convidar_ana=convidar_ana)
                created.append(f"{title} {fmt_pt(d, start)}")
                log(f"  calendário: criado {title} {fmt_pt(d, start)}")
        except Exception as e:
            problems.append(f"Calendário: {e}")
            traceback.print_exc()

    # ----- avisos
    if separados:
        whatsapp("⚠️ BOT ACADEMIA — vocês não estão juntos na aula\n" + "\n".join(separados)
                 + "\nDecidam se vão assim, se esperam a outra vaga ou se desmarcam no app.")
    if waitlisted:
        lines = ["⏳ BOT ACADEMIA — lista de espera" + (" (teste)" if DRY_RUN else "")]
        for label, names in waitlisted:
            lines.append(f"📝 {label} — {' e '.join(names)} na lista de espera")
        lines.append("Se abrir vaga, a academia promove vocês; o robô cria o evento no calendário quando aparecer como inscrito.")
        whatsapp("\n".join(lines))
    if booked:
        lines = ["🏋️ BOT ACADEMIA — reserva feita" + (" (teste)" if DRY_RUN else "")]
        for label, names in booked:
            lines.append(f"✅ {label} — {' e '.join(names)}")
        if created:
            lines.append("📅 Evento(s) no calendário Academia com convite para a Ana Elisa.")
        whatsapp("\n".join(lines))
    if problems:
        log("PROBLEMAS:\n  " + "\n  ".join(problems))
        now = datetime.now(TZ)
        # execução manual: avisa sempre; automática: no máximo 1 aviso por dia (execução das 8h00–8h05)
        if NOTIFY_ERRORS or (now.hour == 8 and now.minute < 6):
            whatsapp("⚠️ BOT ACADEMIA — problema no robô:\n" + "\n".join(problems[:5]), only_jaime=True)

    log(f"Fim — reservas novas: {len(booked)}, listas de espera: {len(waitlisted)}, eventos criados: {len(created)}, problemas: {len(problems)}")
    if problems and NOTIFY_ERRORS:
        sys.exit(1)  # só marca falha nas execuções manuais, para não lotar seu e-mail


if __name__ == "__main__":
    main()
