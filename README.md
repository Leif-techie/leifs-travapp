# Travprojekt

Visar svenska travlopp med aktuell startlista där loppet har `voltstart`, filtrerat till startnummer `1` och `3`.

## Lokal start

```bash
pip install -r requirements.txt
python app.py
```

Öppna sedan `http://localhost:5000`.

## Publicera på Render

1. Lägg projektet i ett GitHub-repo.
2. Logga in på [Render](https://render.com/).
3. Välj `New +` -> `Blueprint`.
4. Koppla ditt GitHub-repo.
5. Render läser automatiskt `render.yaml` och skapar webbappen.
6. När bygget är klart får du en publik URL som din vän kan öppna.

## Viktigt

- Appen uppdaterar data automatiskt var tredje timme.
- `gunicorn --workers 1` används för att undvika dubbla bakgrundstrådar i drift.
- Din vän behöver bara den publika länken, inget Python installerat lokalt.
