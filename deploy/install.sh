#!/bin/bash
# Jednorázová instalace Glukorádce na čistý Ubuntu server (Oracle Cloud Always Free apod.).
#
#   curl -fsSL https://raw.githubusercontent.com/UZIVATEL/glukoradce/main/deploy/install.sh | sudo bash
#
# Skript: nainstaluje Docker a git, stáhne repozitář do /opt/glukoradce, zeptá se na heslo
# do aplikace, otevře porty 80/443 ve firewallu, spustí aplikaci s HTTPS a nastaví
# automatickou aktualizaci z GitHubu každých 5 minut.
set -euo pipefail

REPO_DEFAULT="${GLUKORADCE_REPO:-}"
DIR=/opt/glukoradce

if [ "$(id -u)" -ne 0 ]; then echo "Spusťte přes sudo."; exit 1; fi

echo "== Glukorádce: instalace =="
if [ -z "$REPO_DEFAULT" ]; then
  # když je skript spuštěn přes curl z GitHubu, odvodíme adresu repozitáře z URL
  read -r -p "Adresa GitHub repozitáře (např. https://github.com/uzivatel/glukoradce): " REPO_DEFAULT < /dev/tty
fi
REPO="$REPO_DEFAULT"

read -r -s -p "Zvolte heslo pro přihlášení do Glukorádce: " APP_PW < /dev/tty; echo
if [ ${#APP_PW} -lt 8 ]; then echo "Heslo musí mít aspoň 8 znaků."; exit 1; fi

PUBIP=$(curl -fsS https://api.ipify.org || curl -fsS https://ifconfig.me)
DOMAIN="${GLUKORADCE_DOMAIN:-${PUBIP}.sslip.io}"

echo "-- balíčky (docker, git)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git iptables-persistent >/dev/null
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh >/dev/null
fi

echo "-- firewall: porty 80 a 443"
iptables -I INPUT 5 -p tcp --dport 80 -j ACCEPT 2>/dev/null || true
iptables -I INPUT 5 -p tcp --dport 443 -j ACCEPT 2>/dev/null || true
netfilter-persistent save >/dev/null 2>&1 || true

echo "-- stahuji aplikaci z $REPO"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull -q
else
  git clone -q "$REPO" "$DIR"
fi
cd "$DIR"
cat > .env <<ENV
GLUKORADCE_PASSWORD=$APP_PW
DOMAIN=$DOMAIN
ENV
chmod 600 .env

echo "-- spouštím (první sestavení trvá 1–3 minuty)"
docker compose up -d --build

echo "-- automatická aktualizace z GitHubu každých 5 minut"
chmod +x deploy/update.sh
{ crontab -l 2>/dev/null | grep -v glukoradce/deploy/update.sh || true; echo "*/5 * * * * $DIR/deploy/update.sh >> /var/log/glukoradce-update.log 2>&1"; } | crontab - || true

echo
echo "================================================================"
echo " Hotovo. Za ~1 minutu (vydání HTTPS certifikátu) otevřete:"
echo "   https://$DOMAIN"
echo " Přihlaste se zvoleným heslem a v Nastavení zadejte Dexcom/Tandem."
echo "================================================================"
