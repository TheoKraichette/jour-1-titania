# Bureau d'Analyse Terrestre — relevés Klaxo-3

Analyse de 88 875 signalements d'observations, du chargement du fichier brut
jusqu'au tri automatique des canulars. Les résultats sont dans `RAPPORT.md`.

## Lancer

```bash
pip install -r requirements.txt
python analyse.py
```

Le script télécharge lui-même la transmission (~15 Mo, non versionnée) et
rejoue les six phases d'une traite.

Avec Docker, sans rien installer :

```bash
docker compose run --rm analyse         # exécution courante
docker compose run --rm machine-neuve   # conteneur vierge, sans montage
```
