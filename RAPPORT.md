# Rapport au Conseil — réception des relevés Klaxo-3

## Phase 1 — Ouvrir la caisse

- Lignes dans le fichier : 88 875
- Lignes chargées : 88 679
- Lignes traitées à part : 196

88 679 + 196 = 88 875.

Le fichier arrive sans en-têtes, je les ai remis depuis le manifeste. J'ai évité les
options de `read_csv` qui réparent les lignes cassées, parce qu'elles les suppriment sans
prévenir. Je lis donc le fichier ligne par ligne et j'écarte tout ce qui n'a pas exactement
11 champs. J'ai aussi compté les sauts de ligne du fichier brut : 88 875 également, donc
aucune ligne n'a été fusionnée ou coupée en route.

Les 196 lignes écartées ont toutes le même défaut, 12 champs au lieu de 11. Le script en
affiche une (ligne 877) : à partir du commentaire tout est décalé d'un cran, le témoignage
se retrouve dans `date_posted`, la date de publication dans `latitude`, et la longitude
déborde en douzième position.

J'ai regardé si je pouvais les recoller. Seulement 22 ont une reconstruction unique, 84 en
ont deux possibles et 90 aucune qui tienne. Sur certaines, `duration_seconds` manque et un
0 traîne avant la forme : les colonnes ne sont pas juste décalées, elles sont dans le
désordre. Les recoller reviendrait à inventer des valeurs, donc je les laisse de côté —
0,22 % du fichier.

## Phase 2 — Rien n'est du bon type

| Champ | Type visé | Valeurs qui ont résisté | Exemples fautifs | Origine (témoin / capteur / transmission) |
|---|---|---|---|---|
|  |  |  |  |  |

Quatre anomalies de nature différente, au minimum, chacune avec son compte exact.

## Phase 3 — Trier les canulars

- La règle, en une phrase :
- Relevés marqués canulars : `…` (`…` %)
- Ce que la règle rate ou attrape à tort :

## Phase 4 — Le premier verdict

- Sur 100 canulars réellement présents, le système en attrape : `…`
- Sur 100 relevés signalés, sont vraiment des canulars : `…`
- Ces nombres sont calculés sur : *(quels relevés, jamais vus à l'entraînement)*

## Phase 5 — Le Conseil ne vous croit pas

| Colonne | Qui écrit l'information | À quel moment | Cette personne savait-elle déjà s'il s'agissait d'un canular ? |
|---|---|---|---|
|  |  |  |  |

Colonnes retirées du modèle :

|  | Avant | Après |
|---|---|---|
| Sur 100 canulars, attrapés |  |  |
| Sur 100 signalés, justes |  |  |

Pourquoi le premier chiffre n'avait pas le droit d'exister (trois lignes) :

## Phase 6 — Le modèle le plus bête du Bureau

- Taux de bonnes réponses du stagiaire (« jamais un canular ») : `…`
- Taux de bonnes réponses du vrai modèle : `…`

La mesure présentée au Conseil, et pourquoi (trois lignes) :

Pourquoi le score du stagiaire ne prouve rien :
