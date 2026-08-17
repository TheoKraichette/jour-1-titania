# Rapport au Conseil — réception des relevés Klaxo-3

## Phase 1 — Ouvrir la caisse

- Lignes dans le fichier : 88 875
- Lignes chargées : 88 679
- Lignes traitées à part : 196

Le fichier arrive sans en-têtes, je les remets depuis le manifeste. J'évite les options de
`read_csv` qui réparent les lignes cassées, parce qu'elles les suppriment sans prévenir : je
lis ligne par ligne et j'écarte tout ce qui n'a pas exactement 11 champs. J'ai aussi compté
les sauts de ligne du fichier brut, 88 875 aussi, donc rien n'a été fusionné en route.

Les 196 écartées ont toutes 12 champs. Le script en affiche une (ligne 877) : tout est
décalé d'un cran à partir du commentaire, et la longitude déborde en douzième position.
J'ai regardé si je pouvais les recoller, mais seulement 22 ont une reconstruction unique,
84 en ont deux possibles et 90 aucune. Ce serait inventer des valeurs, donc je les laisse
de côté — 0,22 % du fichier.

## Phase 2 — Rien n'est du bon type

Je convertis sans rien supprimer : ce qui ne passe pas devient `null` et je le compte.
88 679 lignes en entrée comme en sortie.

| Champ | Type visé | Vides | Ont résisté | Valeurs fautives |
|---|---|---|---|---|
| `duration_seconds` | nombre | 2 | 4 | 2, 8 et 0.5 suivis d'une apostrophe inversée, `2631600` entouré d'espaces |
| `latitude` | nombre | 0 | 1 | `33q.200088` |
| `longitude` | nombre | 0 | 0 | — |
| `datetime` | date + heure | 0 | 1220 | `9/1/2012 24:00` |
| `date_posted` | date | 0 | 0 | — |

Les six autres champs sont du texte et le restent.

Les 1 220 heures `24:00` sont récupérables sans rien inventer : minuit de fin de journée,
c'est 00:00 du lendemain. Je les bascule une fois le comptage fait, donc le tableau garde le
chiffre de la conversion brute et la colonne finit quand même complète. Je ne fais pas pareil
pour `33q.200088` : deviner ce que cachait le `q` serait inventer une coordonnée, je la laisse
vide.

| Anomalie | Compte | D'où ça vient |
|---|---|---|
| lettre au milieu d'une coordonnée | 1 | transmission |
| apostrophe inversée collée à la durée | 3 | témoin |
| espaces autour de la durée | 1 | transmission |
| heure `24:00`, qui n'existe pas | 1220 | capteur |
| entités HTML dans le témoignage (`&#44`) | 35 417 | transmission |
| pays non renseigné | 12 365 | témoin |
| coordonnées à (0, 0) | 1 494 | capteur |

Pour l'origine je regarde qui écrit quoi. Les coordonnées sont calculées et pas tapées, donc
une lettre au milieu vient du transport ; l'apostrophe collée à un chiffre, elle, est une
faute de frappe. Le `24:00` revient 1220 fois : c'est une convention du système qui
horodate, pas une erreur isolée. Les `&#44` sont un encodage ajouté pour que les virgules
des témoignages ne cassent pas le CSV.

Les deux dernières anomalies ne font planter aucune conversion, c'est ce qui les rend
gênantes. Les 1 494 coordonnées à (0, 0) sont des villes comme `turin (italy)` que le
géocodage n'a pas su placer : zéro est un nombre valide, donc rien ne proteste, mais le
point atterrit au large de l'Afrique.

Une seule valeur, `33q.200088`, suffirait à faire basculer toute la colonne `latitude` en
texte si on laissait la bibliothèque deviner les types. Une fois les types corrigés, la
carte que le Conseil demandait se trace sans rien changer d'autre :

![Carte des observations](figures/carte.png)

On reconnaît les États-Unis, l'Europe, le Japon et l'Australie. Le point isolé au large de
l'Afrique, ce sont les 1 494 coordonnées à (0, 0).

## Phase 3 — Trier les canulars

**La règle :** un relevé est un canular si le Bureau a écrit dans sa note qu'il s'agit d'un
canular, ou d'un rapport d'élève qu'il ne peut pas certifier.

- Marqués canulars : **871** sur 88 679, soit **0,98 %** — 781 notés « hoax », 90 notés
  « student report » seulement

Le Bureau annote les dossiers douteux entre doubles parenthèses : `((HOAX??))` devant le
témoignage, ou `((NUFORC Note: Student report. PD))` à la fin. Je ne retiens que ce qui est
écrit dans ces notes. 21 autres relevés contiennent bien le mot « hoax », mais c'est le
témoin qui l'écrit, souvent pour jurer que son observation n'en est pas un.

J'ai regardé ce que le Bureau écrit d'autre. Il annote surtout des méprises — Vénus, Sirius,
une traînée d'avion, un lancement de missile — soit 1 478 relevés. Je ne les compte pas :
le témoin a bien vu quelque chose, il l'a mal identifié, ce n'est pas un mensonge.

Ce que la règle rate : tous les canulars que le Bureau n'a jamais annotés. Elle ne mesure
pas les canulars, elle mesure le travail d'annotation du Bureau. 0,98 % est donc un
plancher, sûrement très en dessous du vrai chiffre.

Ce qu'elle attrape à tort : 622 des 871 notes disent « hoax?? », avec deux points
d'interrogation. C'est un soupçon, pas un verdict, et je les compte quand même comme
canulars.

## Phase 4 — Le premier verdict

- Sur 100 canulars réellement présents, le système en attrape : **100**
- Sur 100 relevés signalés, sont vraiment des canulars : **96**

Ces deux nombres sont calculés sur un quart des relevés, soit **22 170 lignes dont 218
canulars**, mises de côté avant l'entraînement et que le modèle n'a jamais vues. Le tirage
est aléatoire mais stratifié, pour garder la même proportion de canulars des deux côtés, et
la graine est fixée à 0 pour que le découpage soit toujours le même.

Le modèle est une régression logistique. Il reçoit le témoignage entier découpé en mots, la
forme et le pays, la durée, les coordonnées, et le nombre de jours entre l'observation et sa
publication.

Attraper 100 canulars sur 100, ça ne ressemble pas à un vrai résultat. Je regarde d'où ça
vient à la phase suivante.

## Phase 5 — Le Conseil ne vous croit pas

| Ce que le modèle lit | Qui écrit l'information | À quel moment | Savait-elle déjà s'il s'agissait d'un canular ? |
|---|---|---|---|
| `comments` : le témoignage | le témoin | au signalement | non |
| `comments` : la note `((...))` | le Bureau | au traitement, des semaines après | **oui** |
| `shape` | le témoin | au signalement | non |
| `country` | le témoin | au signalement | non |
| `state` | le témoin | au signalement | non |
| `duration_seconds` | le témoin | au signalement | non |
| `datetime` : heure, mois, année | le témoin | au signalement | non |
| longueur et exclamations du récit | le témoin | au signalement | non |
| `latitude` | le géocodage automatique | au traitement | non |
| `longitude` | le géocodage automatique | au traitement | non |
| `delai_jours` (`date_posted` − `datetime`) | le Bureau | à la publication | **oui** |

`comments` compte pour deux lignes parce que deux personnes y écrivent, à deux moments. Je
retire la note du Bureau et le délai de publication, je garde le récit du témoin : lui est
bien là quand le signalement arrive.

|  | Avant | Après |
|---|---|---|
| Sur 100 canulars, attrapés | 100 | 32 |
| Sur 100 signalés, justes | 96 | 3 |
| Relevés dénoncés sur 22 170 | 226 | 2 180 |
| AUC (0,5 = tirage au sort) | 1,000 | 0,718 |

Le premier chiffre n'avait pas le droit d'exister parce que la réponse à deviner était écrite
dans le texte que je donnais à lire : le mot « hoax » de la note servait à la fois à
fabriquer l'étiquette et à la retrouver. Le modèle ne prédisait rien, il recopiait une
conclusion qu'un employé du Bureau avait tirée des semaines plus tôt. Devant un signalement
qui vient d'arriver, cette note n'existe pas et la date de publication non plus : il ne reste
que le récit du témoin, et y repérer un mensonge est autrement plus difficile.

La troisième ligne dit tout : pour attraper ses canulars, le modèle honnête en dénonce 2 180
là où le premier en dénonçait 226.

J'ai vérifié que le nettoyage tenait. Plus aucun relevé étiqueté canular ne contient de
marqueur, et les mots que le modèle juge les plus révélateurs sont devenus du vocabulaire
banal (`alleged`, `claim`, `wonderful`) au lieu de `hoax`. J'ai aussi dû jeter une variable
que j'avais ajoutée : la proportion de majuscules. Calculée sur `comments`, elle valait 2,1
fois plus chez les canulars — parce que `((HOAX??))` est en capitales. C'était encore la note
du Bureau qui rentrait, déguisée en statistique.

Enfin, pour savoir si ce plancher venait de l'information manquante ou de mon choix de
modèle, j'ai construit le meilleur système que je pouvais sans jamais lire le Bureau : un
gradient boosting, avec le témoignage résumé en 120 dimensions. Il monte à **59 attrapés sur
100 et 4 justes sur 100**, AUC 0,813. C'est bien mieux que ma régression logistique, donc une
partie du plancher venait bien du modèle — mais il dénonce quand même 3 279 relevés pour en
trouver 128 vrais. L'écart avec la phase 4 ne se referme pas.

## Phase 6 — Le modèle le plus bête du Bureau

Son système tient en une ligne : répondre « ce n'est pas un canular », toujours.

|  | Bonnes réponses | Canulars attrapés |
|---|---|---|
| Le stagiaire | **99,02 %** | 0 sur 218 |
| Mon modèle (régression logistique) | 89,81 % | 69 sur 218 |
| Mon meilleur modèle (gradient boosting) | 85,38 % | 128 sur 218 |

Le classement s'inverse d'une colonne à l'autre : plus un système attrape de canulars, plus
son taux de bonnes réponses baisse. C'est déjà tout le problème.

**La mesure que je présente au Conseil : le nombre de canulars attrapés, mis en face du
nombre de dossiers à relire.** Le Bureau veut arrêter de perdre du temps sur des signalements
inventés, donc la seule question qui compte est combien on en retrouve, et à quel prix. Le
stagiaire en retrouve zéro : son système ne fait gagner aucune minute à personne. Le mien en
retrouve 128 sur 218 en faisant relire 3 279 dossiers au lieu de 22 170, soit 6 canulars sur
10 pour 85 % de travail en moins.

Pourquoi son 99 % ne prouve rien : ce score ne mesure pas sa capacité à trier, il mesure la
rareté des canulars. Comme ils ne sont que 0,98 % du fichier, répondre non à tout donne
99,02 % de bonnes réponses sans jamais regarder un seul dossier. Appliqué à un fichier où un
relevé sur deux serait un canular, le même système tomberait à 50 %. Son score dépend du
fichier qu'on lui donne, pas de ce qu'il fait.

Il y a d'ailleurs une raison arithmétique pour laquelle je ne pouvais pas le battre sur cette
mesure. Lui se trompe 218 fois, une par canular manqué, et n'accuse personne à tort. Pour
faire mieux, il me faudrait accuser à tort moins souvent qu'à raison, donc une précision
au-dessus de 50 %. J'en suis à 4. Aucun réglage n'y aurait changé quoi que ce soit.

## Phase 7 — Plusieurs témoins, un seul événement

Deux relevés parlent du même événement s'ils partagent **la ville, l'état et le jour de
l'observation**, ou si **leur témoignage est recopié mot pour mot**.

- Événements signalés par plus d'un témoin : **2 418** (5 468 relevés concernés)
- Témoins du plus gros : **56**
- Relevés à cheval sur les deux côtés dans la découpe d'hier : **2 397**

Le plus gros rassemblement est Tinley Park (Illinois), le 31 octobre 2004 — celui-là même que
le conseiller à la cartographie cite dans son annotation. Le script l'affiche en entier, ses
56 témoins alignés, tous du même côté de la nouvelle découpe.

Pour les recopies mot pour mot : 612 relevés, 251 textes distincts. Je ne les traite pas tous
de la même façon. La plupart sont des formulations trop banales pour trancher — « Fireball »
écrit par douze personnes dans douze villes différentes n'est pas un événement, c'est un mot
courant. Je ne regroupe que les textes de plus de 80 caractères, soit 56 relevés, où la
recopie ne peut pas être un hasard. Le Bureau confirme d'ailleurs lui-même sur l'un d'eux :
« One of four reports from same source ».

|  | Avant | Après |
|---|---|---|
| Sur 100 canulars, attrapés | 32 | 30 |
| Sur 100 signalés, justes | 3 | 3 |
| AUC | 0,718 | 0,721 |

Les deux nombres bougent peu. C'est logique : 2 397 relevés étaient mal placés, mais sur
88 679 cela reste 2,7 % du fichier, et surtout mon modèle ne reconnaissait déjà plus grand
chose depuis que la note du Bureau lui a été retirée. La fuite était réelle, son effet ici
est petit — ça ne rend pas la correction facultative, ça montre qu'elle aurait davantage
compté sur un modèle qui s'appuyait plus sur le texte.

## Phase 8 — L'ordre des choses

Je coupe sur **`date_posted`**, la date à laquelle le Bureau a reçu le dossier, et non sur
celle de l'observation. C'est dans cet ordre-là que les dossiers arrivent réellement au
Bureau, et surtout c'est dans cet ordre qu'il les annote : comme mon étiquette vient de ses
notes, couper sur la date d'observation laisserait le modèle apprendre d'annotations écrites
après celles du test. Je coupe aussi par événement, pour ne pas défaire la phase 7.

- Date de coupure : **10 octobre 2011**

|  | Apprentissage | Test |
|---|---|---|
| Relevés | 66 509 | 22 170 |
| Canulars | 707 | 164 |
| Proportion de canulars | **1,06 %** | **0,74 %** |

|  | Avant | Après |
|---|---|---|
| Sur 100 canulars, attrapés | 30 | 34 |
| Sur 100 signalés, justes | 3 | 2 |
| AUC | 0,721 | **0,616** |

Les deux proportions ne sont pas égales, et en regardant année par année on comprend
pourquoi : les canulars n'ont pas diminué, c'est le Bureau qui a changé de pratique. Avant
2004 il n'annotait presque rien — zéro canular sur 982 relevés en 1998 — les notes
apparaissent en 2004, culminent vers 2008 à 2,85 %, puis retombent à 0,48 % en 2012.

Mon étiquette ne mesure donc pas la fréquence des canulars mais le rythme de travail des
annotateurs, et ce rythme change dans le temps. C'est ce qui explique la chute de l'AUC de
0,721 à 0,616 : le modèle a appris ce qu'était un canular pendant les années fastes de
l'annotation, et on le note sur une période où le Bureau annotait deux fois moins.
