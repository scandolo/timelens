#!/bin/zsh
# Regenerates every image in docs/images: the reports from the demo week and the setup page from a clean,
# fake home folder (so nothing from your own machine ends up in a screenshot). Needs Google Chrome. No API calls.
set -e
cd ${0:A:h}/..
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
TMP=$(mktemp -d) PROFILE=$(mktemp -d)
trap 'kill $SERVER 2>/dev/null; rm -rf $TMP $PROFILE' EXIT
.venv/bin/python timelens.py demo $TMP/demo >/dev/null
DAY=$(ls $TMP/demo/reports | grep -v week) WEEK=$(ls $TMP/demo/reports | grep week)

shot() {  # scheme(light|dark) width height name url
  local s=$([[ $1 == dark ]] && echo 0 || echo 1)
  perl -e 'alarm 40; exec @ARGV' "$CHROME" --headless=new --disable-gpu --no-first-run --user-data-dir=$PROFILE \
    --hide-scrollbars --force-device-scale-factor=2 --blink-settings=preferredColorScheme=$s --virtual-time-budget=4000 \
    --window-size=$2,$3 --screenshot=$PWD/docs/images/$4-$1.png "$5" >/dev/null 2>&1
}

# a clean copy of the repo (no config.local.toml) and a fake home with only the demo projects
git ls-files -co --exclude-standard | tar -cf - -T - | tar -xf - -C $TMP && mkdir -p $TMP/home/code/{pebble-api,tidepool,field-notes}
ln -s $PWD/.venv $TMP/.venv
(cd $TMP && HOME=$TMP/home TIMELENS_NO_BROWSER=1 .venv/bin/python onboarding.py >/dev/null 2>&1) & SERVER=$!
for i in {1..30}; do curl -s -o /dev/null http://127.0.0.1:8766/ && break; perl -e 'select(undef,undef,undef,0.2)'; done

mkdir -p docs/images
for scheme in light dark; do
  shot $scheme 820 735 day file://$TMP/demo/reports/$DAY
  shot $scheme 820 830 week file://$TMP/demo/reports/$WEEK
  shot $scheme 860 640 setup-goal "http://127.0.0.1:8766/#preset=deep_work&step=0"
  shot $scheme 1120 980 setup-categories "http://127.0.0.1:8766/#preset=deep_work&step=1"
  shot $scheme 860 640 setup-agent "http://127.0.0.1:8766/#preset=deep_work&step=2&tab=agent"
  shot $scheme 860 760 setup-model "http://127.0.0.1:8766/#preset=deep_work&step=3"
  shot $scheme 1120 1000 setup-preview "http://127.0.0.1:8766/#preset=deep_work&step=6"
done
for scheme in light dark; do  # the hero embeds the day screenshot, so render it after
  shot $scheme 1280 640 hero file://$PWD/docs/src/hero.html
  shot $scheme 1280 395 how file://$PWD/docs/src/how.html
done
ls docs/images
