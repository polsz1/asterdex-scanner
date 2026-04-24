# AsterDex Volatility Scanner

Automatyczny skaner tokenów na AsterDex uruchamiany co godzinę przez GitHub Actions.

## Pliki

| Plik | Opis |
|------|------|
| `skaner5.py` | Główny skaner |
| `generate_report.py` | Generator strony HTML |
| `requirements.txt` | Zależności Python |
| `.github/workflows/scan.yml` | Harmonogram GitHub Actions |
| `aster_history.csv` | Historia runów (auto-commitowana) |
| `aster_oi_cache.json` | Cache Open Interest (auto-commitowany) |
| `docs/index.html` | Strona z wynikami (GitHub Pages) |

## Instalacja

### 1. Utwórz repo na GitHub
- Nowe repo → **Private**
- Wrzuć wszystkie pliki

### 2. Włącz GitHub Pages
- Settings → Pages
- Source: **Deploy from a branch**
- Branch: `main` / folder: `/docs`
- Zapisz

### 3. Włącz zapis do repo przez Actions
- Settings → Actions → General
- Scroll do **Workflow permissions**
- Wybierz **Read and write permissions**
- Zapisz

### 4. Pierwsze uruchomienie
- Zakładka **Actions** → `AsterDex Scanner` → **Run workflow**

### 5. Strona z wynikami
Po pierwszym runie dostępna pod:
```
https://TWOJ_LOGIN.github.io/NAZWA_REPO/
```

## Harmonogram

Domyślnie co godzinę (minuta 0, UTC). Zmień w `.github/workflows/scan.yml`:
```yaml
cron: '0 * * * *'   # co godzinę
cron: '*/30 * * * *' # co 30 minut
cron: '0 */2 * * *'  # co 2 godziny
```

## Ręczne uruchomienie

Actions → AsterDex Scanner → Run workflow
