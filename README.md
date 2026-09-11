# Glukorádce

Osobní bolusový poradce nad **Dexcom G7** a pumpou **Tandem t:slim X2 (Control-IQ)**.
Běží jako malý webový server na Macu (nebo Raspberry Pi), na iPhonu se používá
jako webová aplikace přidaná na plochu.

> **Důležité:** Glukorádce je osobní pomůcka, ne zdravotnický prostředek. Nikdy nic
> nedávkuje – jen navrhuje a vysvětluje výpočet. Dávku vždy zadáváte vy na pumpě.
> Parametry (ICR, ISF, cíl) a způsob krytí tuků/bílkovin konzultujte s diabetologem.

## Co umí (verze 1)

- **Glykémie v reálném čase** z Dexcom Share (každých 5 min), graf 3–24 h, trend, aktivní inzulín.
- **Databáze jídel** (sacharidy, tuky, bílkoviny na porci) – „Pizza 1 celá“, „Těstoviny talíř“…
- **Návrh dávky ve dvou krocích**: hned (sacharidy + korekce podle glykémie, trendu a IOB)
  a **druhá dávka za ~2 h** na tuky a bílkoviny (tuko-bílkovinné jednotky, tzv. varšavská metoda).
  Control-IQ nedovolí prodloužený bolus delší než 2 h, proto je druhá dávka samostatný bolus –
  aplikace na něj připomene na hlavní obrazovce.
- **Učení z výsledků**: 6 h po jídle se z CGM křivky vyhodnotí vrchol, glykémie ve 2 h a 5 h, hypa,
  čas v cíli, a **pro toto konkrétní jídlo** se upraví násobitel první dávky, druhé dávky a její
  načasování. Při další pizze uvidíte: „minule 10 j. + 4,5 j.: vrchol 12,4, v 5 h 4,1 → hypo, druhá
  dávka byla moc“ a nový návrh.
- **Bezpečnostní limity**: strop jedné dávky a korekce, změna naučených parametrů max. ±10 % na jedno
  jídlo, násobitele v mezích 0,6–1,5, po hyperglykémii se nikdy nesnižuje, po hypoglykémii nikdy
  nezvyšuje, neučí se z jídel, která se překrývají s jiným jídlem nebo mají divnou výchozí glykémii.
- **Import bolusů z pumpy** (CSV z Tandem Source) – včetně automatických korekcí Control-IQ, které
  se započítají do IOB a do učení („pumpa musela přidat 1,3 j.“ = dávka byla malá).
- Demo režim se simulovanými daty.

## Spuštění na Macu

```bash
cd glukoradce
./start.sh            # vytvoří .venv, nainstaluje závislosti, spustí server
# nebo: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/python app.py
```

Otevřete <http://localhost:8765>. Na iPhonu ve stejné Wi-Fi otevřete v Safari
`http://<IP-Macu>:8765` (IP zjistíte v Nastavení → Wi-Fi) a přes **Sdílet → Přidat na plochu**
si ji uložíte jako aplikaci.

Vyzkoušení bez Dexcomu: `./start.sh --demo` (3 dny simulovaných dat a 3 pizzy s vyhodnocením).

### Nastavení Dexcomu

1. V aplikaci Dexcom G7 zapněte **Sdílení** (Share) – stačí zapnuté, nemusíte nikoho zvát.
2. V Glukorádci → Nastavení zadejte **uživatelské jméno a heslo k účtu Dexcom** (ne Follow),
   region „Evropa“. Uložit → „Stáhnout data teď“.

Používá se stejné rozhraní jako aplikace Dexcom Follow (knihovna `pydexcom`). Není to oficiální
API – Dexcom ho může změnit; pro osobní použití je to ale nejjednodušší cesta k datům v reálném čase.

### Nastavení terapie

Do Nastavení opište z pumpy (profil Control-IQ) **ICR** (g sacharidů na jednotku), **ISF**
(mmol/l na jednotku) a cíl. Pokud máte během dne různé hodnoty, lze zadat segmenty přímo v databázi
nastavení (`segments`), viz `glukoradce/db.py` – v další verzi bude i v UI.

**Krytí FPU** začíná na 50 % (konzervativně, Control-IQ část pozdního vzestupu pokryje sama).
Učení si to pro každé jídlo doladí.

### Bolusy z pumpy

Aplikace potřebuje vědět, kolik jste si opravdu dal:

- **Ručně**: při zapsání jídla vyplníte podanou dávku (předvyplní se návrh), druhou dávku potvrdíte
  na hlavní obrazovce, korekce mimo jídlo zapíšete v poli „Zapsat bolus mimo jídlo“.
- **Automaticky z Tandem Source** (doporučeno): Nastavení → Tandem Source → e-mail a heslo účtu
  t:connect, „Otestovat připojení“. Používá knihovnu `tconnectsync`; bolusy a automatické korekce
  Control-IQ se pak stahují každých 5 min. Bolus, který jste už zapsal ručně v aplikaci, se s tím
  z pumpy spáruje (±20 min, ±0,35 j.), aby se nepočítal dvakrát.
  Diagnostika z Terminálu: `.venv/bin/python tandem_probe.py` – vypíše, co Tandem vrací, a uloží
  `data/tandem_probe.json` (bez hesla) pro doladění parseru, kdyby se formát dat změnil.
- **Import CSV** z Tandem Source (Reports → export) – Nastavení → Import. Záloha pro případ, že
  automatický sync nefunguje. Duplicity se ignorují.

## Provoz zdarma na Oracle Cloud (Always Free) – běží 24/7 nezávisle na Macu

Oracle nabízí trvale bezplatný malý virtuální server. Aplikace na něm běží v Dockeru
s automatickým HTTPS (Caddy + Let's Encrypt, doména `<IP>.sslip.io` zdarma) a každých
5 minut si sama stahuje aktualizace z vašeho GitHub repozitáře.

**A. Kód na GitHub (jednou)**
Založte repozitář `glukoradce` (může být *Public* – kód neobsahuje žádná hesla ani data; u
*Private* je potřeba na serveru token, viz níže) a nahrajte do něj obsah projektu bez složek
`data/` a `.venv/` (web GitHubu: Add file → Upload files, přetáhnout z Finderu).

**B. Server na Oracle (jednou, ~15 minut klikání)**
1. oracle.com/cloud/free → *Start for free*. Registrace chce platební kartu na ověření
   totožnosti; Always Free zdroje se neúčtují. Domovský region zvolte **Germany Central (Frankfurt)**.
2. V konzoli: **Compute → Instances → Create instance**.
   - Image: **Canonical Ubuntu 24.04** (Change image).
   - Shape: **Always Free-eligible** – `VM.Standard.A1.Flex` (1 OCPU, 6 GB) nebo
     `VM.Standard.E2.1.Micro`. (Když A1 hlásí *Out of capacity*, vezměte E2.1.Micro.)
   - Add SSH keys: **Generate a key pair for me** → *Save private key* (stáhne se `ssh-key-….key`).
   - Create. Po minutě uvidíte **Public IP address** – opište si ji.
3. Otevřít porty: na stránce instance klikněte na **Subnet** → **Security List** (Default…)
   → **Add Ingress Rules**: Source CIDR `0.0.0.0/0`, IP Protocol TCP, Destination Port Range
   `80,443` → Add.

**C. Instalace (jednou, 2 příkazy v Terminálu na Macu)**
```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@VEREJNA_IP
```
(při dotazu *Are you sure you want to continue connecting?* napište `yes`). Na serveru pak:
```bash
curl -fsSL https://raw.githubusercontent.com/UZIVATEL/glukoradce/main/deploy/install.sh | sudo bash
```
Skript se zeptá na adresu repozitáře a na heslo do aplikace, nainstaluje Docker, spustí
Glukorádce a vypíše adresu **https://VEREJNA_IP.sslip.io**. Tu otevřete (certifikát se vydá do
minuty), přihlaste se, v Nastavení zadejte Dexcom, Tandem a parametry, a na iPhonu přidejte na plochu.

Aktualizace: stačí nahrát změněné soubory do repozitáře na GitHubu – server je do 5 minut
převezme a aplikaci sám znovu sestaví. Log: `/var/log/glukoradce-update.log`, stav:
`cd /opt/glukoradce && sudo docker compose logs --tail 50`.

*Soukromý repozitář:* vytvořte na GitHubu *fine-grained personal access token* jen pro čtení
repozitáře a při instalaci zadejte adresu ve tvaru `https://TOKEN@github.com/UZIVATEL/glukoradce`.

## Provoz v cloudu (Railway, placené ~5 USD/měs.)

Aplikace je připravená jako Docker kontejner (`Dockerfile`, `railway.json`). Databáze žije na
trvalém disku připojeném do `/data`. Přístup chrání heslo (`GLUKORADCE_PASSWORD`) a HTTPS.

1. **GitHub**: založte nový **soukromý** repozitář (např. `glukoradce`) a nahrajte do něj obsah
   složky projektu – *bez* složek `data/` a `.venv/` (obsahují vaše hesla a lokální knihovny).
   Web GitHubu umí „Add file → Upload files“ přetažením souborů z Finderu.
2. **Railway** (railway.app): New Project → Deploy from GitHub repo → vyberte repozitář.
   Railway pozná Dockerfile a sestaví aplikaci.
3. V projektu otevřete službu → **Variables** a přidejte:
   - `GLUKORADCE_PASSWORD` = heslo, kterým se budete přihlašovat do Glukorádce (zvolte silné).
4. **Volume**: ve službě → Settings (nebo pravým tlačítkem na službu) → *Add Volume*,
   mount path **`/data`**. Bez toho by se databáze při každém nasazení smazala.
5. Settings → Networking → **Generate Domain** – dostanete adresu typu
   `glukoradce-production.up.railway.app`. Otevřete ji, přihlaste se heslem a v Nastavení
   zadejte účty Dexcom a Tandem a terapeutické parametry (na serveru začínáte s čistou databází).
6. Na iPhonu adresu otevřete v Safari a přes Sdílet → Přidat na plochu uložte jako aplikaci.

Aktualizace: nahrajte změněné soubory do repozitáře na GitHubu – Railway je automaticky znovu
nasadí (typicky do 2 minut). Cena: Hobby plán ~5 USD/měsíc, aplikace spotřebuje zlomek.

Lokální běh na Macu funguje dál beze změny (`./start.sh`); heslo se vyžaduje jen když je
nastavená proměnná `GLUKORADCE_PASSWORD`.

## Jak funguje učení

Pro každé jídlo se drží tři parametry: `now_mult` (násobitel sacharidové dávky), `late_mult`
(násobitel dávky na tuky/bílkoviny) a `late_delay_min` (odstup druhé dávky).

Po 6 hodinách od jídla:

1. Z CGM se vezme glykémie ve 2 h (odpověď na první dávku) a v 5 h (odpověď na druhou dávku),
   minimum do 3 h a po 3 h (hypa), vrchol.
2. Zpětně se odhadne „ideální“ dávka: `podaná + 0,5 × (glykémie − cíl) / ISF` (při hypu
   `podaná − 0,7 × (cíl − minimum) / ISF`). Automatické korekce pumpy se přičtou k podané dávce.
3. Násobitel se posune k odhadu, nejvýš o `learn_step` (0,10). Po vysoké křivce se nikdy nesnižuje,
   po hypu nikdy nezvyšuje (odhad je tam jen mez, ne přesná hodnota).
4. Načasování: vzestup už ve 3 h (2 h v pořádku) → druhá dávka o 15 min dřív; hypo krátce po druhé
   dávce → o 15 min později.

Vše je vidět v Historii („první dávka 9,5 j. → odhad ideálu 10,8 j., násobitel 1,00 → 1,10“).
Parametry jídla lze vynulovat (`POST /api/meals/<id>/reset`).

## Struktura

```
app.py                    Flask server + synchronizace na pozadí (každých 5 min)
glukoradce/db.py          SQLite (glykémie, jídla, události jídel, bolusy, parametry, nastavení)
glukoradce/dexcom_client.py  Dexcom Share (pydexcom nebo vestavěný klient)
glukoradce/iob.py         aktivní inzulín (exponenciální křivka, DIA 5 h, vrchol 75 min)
glukoradce/recommender.py návrh dávky (sacharidy, korekce, FPU, naučené násobitele, stropy)
glukoradce/outcomes.py    vyhodnocení jídla z CGM + učení
glukoradce/tandem_sync.py    automatický sync bolusů z Tandem Source (tconnectsync)
glukoradce/tandem_import.py  import CSV z Tandem Source
tandem_probe.py           diagnostika připojení k Tandemu
glukoradce/demo.py        simulátor glykémie pro demo
static/index.html         webová aplikace (PWA pro iPhone)
tests/test_core.py        testy: python3 tests/test_core.py
data/glukoradce.sqlite    vaše data (vznikne při prvním spuštění)
```

## Plán dalších verzí

- Segmenty ICR/ISF podle denní doby v UI, editace jídel.
- Push připomínka druhé dávky (Web Push pro iOS ≥ 16.4).
- Statistický model (více jídel, výchozí glykémie, denní doba, pohyb) až bude ≥ 30 vyhodnocených jídel.
- Export dat (CSV) pro diabetologa.
