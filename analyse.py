"""Bureau d'Analyse Terrestre — relevés de la sonde Klaxo-3.

Télécharge la transmission et rejoue les six phases d'une traite.

    python analyse.py
"""

import csv
import os
import re
import urllib.request
from collections import Counter

import matplotlib
import numpy as np
import polars as pl
from scipy.sparse import csr_matrix, hstack
from sklearn.decomposition import TruncatedSVD
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (accuracy_score, precision_score, recall_score,
                             roc_auc_score)
from sklearn.model_selection import StratifiedGroupKFold, train_test_split
from sklearn.preprocessing import OneHotEncoder, StandardScaler

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

URL = (
    "https://raw.githubusercontent.com/planetsig/ufo-reports/master/csv-data/"
    "ufo-complete-geocoded-time-standardized.csv"
)
CSV = "releves_klaxo3.csv"

# Le fichier est livré sans en-têtes.
COLONNES = [
    "datetime",
    "city",
    "state",
    "country",
    "shape",
    "duration_seconds",
    "duration_hours_min",
    "comments",
    "date_posted",
    "latitude",
    "longitude",
]


def titre(n, nom):
    print(f"\n{'=' * 70}\nPHASE {n} — {nom}\n{'=' * 70}")


def recuperer_la_transmission():
    if os.path.exists(CSV):
        print(f"{CSV} déjà présent ({os.path.getsize(CSV) / 1e6:.1f} Mo)")
        return
    print(f"Téléchargement depuis {URL}")
    urllib.request.urlretrieve(URL, CSV)
    print(f"Reçu : {os.path.getsize(CSV) / 1e6:.1f} Mo")


def compter_lignes_physiques(chemin):
    with open(chemin, "rb") as f:
        return sum(bloc.count(b"\n") for bloc in iter(lambda: f.read(1 << 20), b""))


DATE_SEULE = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def _numerique(valeur):
    try:
        float(valeur)
        return True
    except ValueError:
        return False


def reconstructions(champs):
    """Reconstructions distinctes d'une ligne trop longue, en retirant un champ vide.

    Une reconstruction n'est retenue que si les champs repères retombent sur leurs
    pieds : date de publication, coordonnées et durée.
    """
    trouvees = set()
    for i, valeur in enumerate(champs):
        if valeur != "":
            continue
        candidat = tuple(champs[:i] + champs[i + 1 :])
        if len(candidat) != len(COLONNES):
            continue
        if (
            DATE_SEULE.match(candidat[8])
            and _numerique(candidat[9])
            and _numerique(candidat[10])
            and (candidat[5] == "" or _numerique(candidat[5]))
        ):
            trouvees.add(candidat)
    return trouvees


def phase1_ouvrir_la_caisse():
    """Charger tout le fichier. Rendre : lignes du fichier, chargées, mises à part."""
    titre(1, "ouvrir la caisse")

    lignes_physiques = compter_lignes_physiques(CSV)

    # Parcours manuel plutôt qu'un read_csv tolérant : on veut garder les lignes
    # rejetées sous la main, pas les laisser disparaître.
    conformes, rejets = [], []
    with open(CSV, encoding="utf-8", errors="replace", newline="") as f:
        for numero, champs in enumerate(csv.reader(f), start=1):
            if len(champs) == len(COLONNES):
                conformes.append(champs)
            else:
                rejets.append((numero, champs))

    total = len(conformes) + len(rejets)
    df = pl.DataFrame(
        conformes, schema={col: pl.String for col in COLONNES}, orient="row"
    )

    print(f"Lignes physiques (\\n)      : {lignes_physiques}")
    print(f"Enregistrements CSV        : {total}")
    print(f"Chargés (11 champs)        : {df.height}")
    print(f"Mis à part                 : {len(rejets)}")
    print(f"Cohérence                  : {df.height} + {len(rejets)} = {total}")

    if rejets:
        print("\nCe qu'il y a dans les lignes mises à part :")
        for nb_champs, combien in sorted(Counter(len(c) for _, c in rejets).items()):
            print(f"  {combien} ligne(s) à {nb_champs} champs au lieu de {len(COLONNES)}")

        numero, champs = rejets[0]
        print(f"\nExemple, ligne {numero} ({len(champs)} champs) :")
        for i, valeur in enumerate(champs):
            nom = COLONNES[i] if i < len(COLONNES) else "— en trop —"
            print(f"  [{i:2}] {nom:<19} {valeur[:60]}")

        # Peut-on les recoller en retirant le champ vide en trop ?
        combien = Counter(len(reconstructions(c)) for _, c in rejets)
        uniques = combien[1]
        ambigues = sum(n for k, n in combien.items() if k > 1)
        print(
            f"\nRecollage : {uniques} lignes ont une reconstruction unique, "
            f"{ambigues} en ont plusieurs, {combien[0]} aucune."
        )

    return df


NUMERIQUES = ["duration_seconds", "latitude", "longitude"]


def conversions_de_type(avec_recuperation=True):
    """Les expressions de conversion, sans rien afficher : un relevé neuf doit
    pouvoir emprunter exactement le même chemin que le fichier d'origine."""
    conversions = {col: pl.col(col).cast(pl.Float64, strict=False) for col in NUMERIQUES}
    conversions["datetime"] = pl.col("datetime").str.to_datetime(
        "%m/%d/%Y %H:%M", strict=False
    )
    # date_posted n'a pas d'heure : une vraie date, pas un datetime à minuit.
    conversions["date_posted"] = pl.col("date_posted").str.to_date(
        "%m/%d/%Y", strict=False
    )
    if avec_recuperation:
        conversions["datetime"] = (
            pl.when(pl.col("datetime").str.contains(" 24:"))
            .then(
                pl.col("datetime")
                .str.replace(" 24:", " 00:")
                .str.to_datetime("%m/%d/%Y %H:%M", strict=False)
                .dt.offset_by("1d")
            )
            .otherwise(conversions["datetime"])
        )
    return conversions


def preparer(df):
    """Du relevé brut aux variables du modèle. Aucun apprentissage ici : que des
    règles fixes, applicables à une ligne comme à 88 000."""
    df = df.with_columns(**conversions_de_type())
    return ajouter_indicateurs(ajouter_temoignage(ajouter_delai(df)))


def phase2_typer(df):
    """Chaque champ dans son vrai type, sans supprimer de ligne."""
    titre(2, "rien n'est du bon type")

    conversions = conversions_de_type(avec_recuperation=False)

    print(f"{'champ':<18} {'vides':>6} {'résistent':>10}   valeurs fautives")
    for col, expr in conversions.items():
        brut = df[col]
        converti = df.select(expr).to_series()
        vide = brut.str.strip_chars() == ""
        resiste = ~vide & converti.is_null()
        fautives = brut.filter(resiste).unique().head(4).to_list()
        print(f"{col:<18} {vide.sum():>6} {resiste.sum():>10}   {fautives}")

    compte = lambda expr: int(df.select(expr).to_series().sum())  # noqa: E731
    duree = df["duration_seconds"]
    natures = {
        "lettre parasite dans latitude": df["latitude"].str.contains("[A-Za-z]"),
        "apostrophe inversée collée à la durée": duree.str.contains("`"),
        "espaces autour de la durée": (duree != duree.str.strip_chars()) & (duree != ""),
        "heure 24:00, qui n'existe pas": df["datetime"].str.contains(" 24:"),
        "entités HTML dans le témoignage": df["comments"].str.contains("&#"),
        "pays non renseigné": df["country"].str.strip_chars() == "",
    }
    # Celle-ci passe la conversion sans broncher : zéro est un nombre valide.
    lat = df.select(conversions["latitude"]).to_series()
    lon = df.select(conversions["longitude"]).to_series()
    natures["coordonnées à (0, 0), géocodage raté"] = (lat == 0) & (lon == 0)
    print("\nAnomalies par nature :")
    for nom, masque in natures.items():
        print(f"  {nom:<40} {masque.sum():>6}")

    # 24:00 = minuit de fin de journée, donc 00:00 du lendemain. Récupérable sans
    # rien inventer, contrairement au « 33q » de la latitude qu'on laisse en null.
    minuit_fin = pl.col("datetime").str.contains(" 24:")
    print(f"\nRécupéré : {compte(minuit_fin)} heures 24:00 basculées au lendemain 00:00.")

    avant = df.height
    df = df.with_columns(**conversions_de_type())
    print(f"\nLignes : {avant} en entrée, {df.height} en sortie.")
    print("Type final de chaque champ :")
    for nom, type_final in df.schema.items():
        print(f"  {nom:<20} {type_final}")

    carte_des_observations(df)
    return df


def carte_des_observations(df):
    """La carte que le Conseil n'arrivait pas à tracer, une fois les types corrigés."""
    points = df.select("latitude", "longitude").drop_nulls()
    os.makedirs("figures", exist_ok=True)

    fig, ax = plt.subplots(figsize=(13, 6.5))
    ax.scatter(points["longitude"], points["latitude"], s=1, alpha=0.15, linewidths=0)
    ax.set_xlabel("longitude")
    ax.set_ylabel("latitude")
    ax.set_title(f"{points.height} observations situées")
    fig.savefig("figures/carte.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print(f"\nCarte : figures/carte.png ({points.height} points)")


def phase3_etiqueter_les_canulars(df):
    """Fabriquer l'étiquette « canular » : aucun champ ne la donne."""
    titre(3, "trier les canulars")

    bas = pl.col("comments").str.to_lowercase()
    # Le Bureau annote les dossiers douteux entre doubles parenthèses : ((HOAX??)),
    # ((NUFORC Note: Possible hoax?? PD)) ou ((NUFORC Note: Student report. PD)).
    dans_note = bas.str.contains(r"\(\([^)]*(?:hoax|student report)")
    df = df.with_columns(canular=dans_note)

    marques = int(df["canular"].sum())
    print("Règle : dans sa note, le Bureau parle de « hoax » ou de « student report ».")
    print(f"Marqués canulars : {marques} / {df.height} ({marques / df.height:.2%})")

    compte = lambda expr: int(df.select(expr).to_series().sum())  # noqa: E731
    hoax = bas.str.contains(r"\(\([^)]*hoax")
    print(f"  dont « hoax »                                    : {compte(hoax)}")
    print(f"  dont « student report » seul                     : {compte(dans_note & ~hoax)}")
    print(f"  « hoax » écrit par le témoin, hors note, écarté  : "
          f"{compte(bas.str.contains('hoax', literal=True) & ~hoax)}")
    print(f"  parmi les marqués, « hoax?? », donc un doute     : "
          f"{compte(bas.str.contains('hoax??', literal=True) & dans_note)}")

    # Une méprise n'est pas un canular : le témoin a vu quelque chose, il l'a mal
    # identifié. Ces notes-là ne comptent pas.
    meprises = bas.str.contains(
        r"\(\([^)]*(?:venus|sirius|jupiter|contrail|satellite|meteor|missile"
        r"|advertising|balloon)"
    )
    print(f"  méprises annotées par le Bureau, non comptées    : {compte(meprises)}")

    print("\nExemples marqués :")
    for texte in df.filter(pl.col("canular"))["comments"].head(3).to_list():
        print("  -", texte[:100].replace("&#44", ","))

    return df


COLS_CAT = ["shape", "country", "state"]
COLS_NUM = ["duration_seconds", "latitude", "longitude", "heure", "mois", "annee"]
# Ce que la phase 12 ajoute : la ville, et l'heure posée sur un cercle.
COLS_CAT_FINAL = COLS_CAT + ["city"]
COLS_NUM_FINAL = [c for c in COLS_NUM if c != "heure"] + ["heure_sin", "heure_cos"]
# Deux orthographes pour la même forme. Règle fixe, rien d'appris ici.
FORMES_SYNONYMES = {"changed": "changing", "round": "circle"}
VILLE_RARE = 20
# Dérivées du récit lui-même, pas de son contenu : longueur et emportement.
COLS_STYLE = ["longueur", "exclamations"]
GRAINE = 0


# Les notes du Bureau, y compris celles qui ne sont pas refermées proprement.
NOTE_DU_BUREAU = r"\(\(.*?\)+|\(\(.*$"


def ajouter_delai(df):
    """Jours entre l'observation et la publication du signalement par le Bureau."""
    return df.with_columns(
        delai_jours=(
            pl.col("date_posted").cast(pl.Datetime) - pl.col("datetime")
        ).dt.total_days()
    )


def ajouter_temoignage(df):
    """Sépare ce que le témoin a raconté de ce que le Bureau a ajouté après coup."""
    df = df.with_columns(
        temoignage=pl.col("comments").str.replace_all(NOTE_DU_BUREAU, "")
    )
    # Tout se calcule sur le témoignage seul : mesurer les majuscules ou la longueur
    # sur `comments` ferait rentrer la note du Bureau par la fenêtre.
    # L'heure tourne en rond : 23 h et 0 h sont voisines, pas aux deux bouts d'une
    # règle graduée. Deux coordonnées sur un cercle le disent, un entier ne le dit pas.
    angle = pl.col("datetime").dt.hour() * (2 * np.pi / 24)
    return df.with_columns(
        heure=pl.col("datetime").dt.hour(),
        heure_sin=angle.sin(),
        heure_cos=angle.cos(),
        mois=pl.col("datetime").dt.month(),
        annee=pl.col("datetime").dt.year(),
        longueur=pl.col("temoignage").str.len_chars(),
        exclamations=pl.col("temoignage").str.count_matches("&#33"),
        shape=pl.col("shape").fill_null("").str.to_lowercase().str.strip_chars()
        .replace(FORMES_SYNONYMES),
    )


def decouper(y, groupes=None):
    """Met un quart des relevés de côté. Avec `groupes`, aucun événement n'est coupé
    en deux : tous ses témoins vont du même côté."""
    if groupes is None:
        return train_test_split(
            np.arange(len(y)), test_size=0.25, stratify=y, random_state=GRAINE
        )
    decoupe = StratifiedGroupKFold(n_splits=4, shuffle=True, random_state=GRAINE)
    return next(decoupe.split(np.zeros(len(y)), y, groupes))


def decoupe_temporelle(df, groupes, part_test=0.25):
    """Apprendre sur le passé, être noté sur l'avenir.

    On coupe sur la date de publication, celle qui dit quand le Bureau a reçu le
    dossier, et on coupe par événement : un événement bascule en entier du côté de
    sa première publication, pour ne pas défaire la phase 7.
    """
    par_groupe = (
        pl.DataFrame({"groupe": groupes, "publie": df["date_posted"]})
        .group_by("groupe")
        .agg(pl.col("publie").min().alias("publie"), pl.len().alias("taille"))
        .sort("publie")
    )
    cumul = par_groupe["taille"].cum_sum()
    limite = int(df.height * (1 - part_test))
    au_train = par_groupe.filter(cumul <= limite)["groupe"].to_list()
    date_coupure = par_groupe.filter(cumul > limite)["publie"][0]

    marque = np.zeros(int(groupes.max()) + 1, dtype=bool)
    marque[au_train] = True
    dans_train = marque[groupes]
    return np.flatnonzero(dans_train), np.flatnonzero(~dans_train), date_coupure


def entrainer(df, texte, avec_delai, groupes=None, decoupe=None):
    """Entraîne un modèle et l'évalue sur un quart des relevés, mis de côté d'avance.

    `texte` désigne la colonne de texte donnée au modèle, ou None pour n'en donner aucune.
    """
    y = df["canular"].to_numpy().astype(int)
    i_train, i_test = decoupe if decoupe is not None else decouper(y, groupes)

    blocs_train, blocs_test = [], []

    if texte:
        tfidf = TfidfVectorizer(max_features=20000, min_df=2)
        textes = df[texte].to_list()
        blocs_train.append(tfidf.fit_transform([textes[i] for i in i_train]))
        blocs_test.append(tfidf.transform([textes[i] for i in i_test]))

    cats = df.select(COLS_CAT).fill_null("").to_numpy()
    encodeur = OneHotEncoder(handle_unknown="ignore", min_frequency=20)
    blocs_train.append(encodeur.fit_transform(cats[i_train]))
    blocs_test.append(encodeur.transform(cats[i_test]))

    colonnes = (
        COLS_NUM
        + COLS_STYLE
        + [c for c in COLS_MANQUE if c in df.columns]
        + (["delai_jours"] if avec_delai else [])
    )
    nums = df.select(colonnes).to_numpy().astype(float)
    mediane = np.nanmedian(nums[i_train], axis=0)
    nums = np.where(np.isnan(nums), mediane, nums)
    echelle = StandardScaler()
    blocs_train.append(csr_matrix(echelle.fit_transform(nums[i_train])))
    blocs_test.append(csr_matrix(echelle.transform(nums[i_test])))

    # liblinear plutôt que le solveur par défaut : même résultat, deux fois et demie
    # plus rapide sur une matrice creuse de cette largeur.
    modele = LogisticRegression(solver="liblinear", class_weight="balanced")
    modele.fit(hstack(blocs_train).tocsr(), y[i_train])
    X_test = hstack(blocs_test).tocsr()
    predit = modele.predict(X_test)

    return {
        "rappel": recall_score(y[i_test], predit),
        "precision": precision_score(y[i_test], predit, zero_division=0),
        "exactitude": accuracy_score(y[i_test], predit),
        "auc": roc_auc_score(y[i_test], modele.decision_function(X_test)),
        "n_test": len(i_test),
        "canulars_test": int(y[i_test].sum()),
        "signalements": int(predit.sum()),
        "attrapes": int(((predit == 1) & (y[i_test] == 1)).sum()),
        "y_test": y[i_test],
        "indices_test": i_test,
    }


def phase4_premier_verdict(df):
    """Un modèle, évalué sur des relevés jamais vus : rappel et précision."""
    titre(4, "le premier verdict")

    resultat = entrainer(df, texte="comments", avec_delai=True)
    print(
        f"Test sur {resultat['n_test']} relevés jamais vus à l'entraînement, "
        f"dont {resultat['canulars_test']} canulars."
    )
    print(f"Sur 100 canulars réels, le système en attrape        : "
          f"{resultat['rappel'] * 100:.0f}")
    print(f"Sur 100 relevés signalés, en sont vraiment           : "
          f"{resultat['precision'] * 100:.0f}")
    return resultat


# Une ligne par colonne donnée au modèle. La dernière case est celle qui tranche :
# si la personne qui remplit le champ savait déjà, la colonne sort.
PROVENANCE = [
    ("comments : le témoignage", "témoin", "au signalement", False),
    ("comments : la note ((...))", "Bureau", "au traitement", True),
    ("shape", "témoin", "au signalement", False),
    ("country", "témoin", "au signalement", False),
    ("state", "témoin", "au signalement", False),
    ("duration_seconds", "témoin", "au signalement", False),
    ("datetime : heure, mois, année", "témoin", "au signalement", False),
    ("longueur et exclamations du récit", "témoin", "au signalement", False),
    ("latitude", "géocodage automatique", "au traitement", False),
    ("longitude", "géocodage automatique", "au traitement", False),
    ("delai_jours", "Bureau (date_posted)", "à la publication", True),
]


def phase5_fuite_temporelle(df, avant):
    """Retirer les colonnes remplies après coup, réentraîner, comparer."""
    titre(5, "le Conseil ne vous croit pas")

    print(f"{'ce que le modèle lit':<34} {'qui écrit':<22} {'à quel moment':<16} savait ?")
    for colonne, qui, quand, savait in PROVENANCE:
        print(f"{colonne:<34} {qui:<22} {quand:<16} {'OUI' if savait else 'non'}")

    sortent = [colonne for colonne, _, _, savait in PROVENANCE if savait]
    print(f"\nSortent du modèle : {', '.join(sortent)}")

    apres = entrainer(df, texte="temoignage", avec_delai=False)

    print(f"\n{'':<38}{'avant':>8}{'après':>8}")
    print(f"{'sur 100 canulars réels, attrapés':<38}"
          f"{avant['rappel'] * 100:>8.0f}{apres['rappel'] * 100:>8.0f}")
    print(f"{'sur 100 signalés, vraiment canulars':<38}"
          f"{avant['precision'] * 100:>8.0f}{apres['precision'] * 100:>8.0f}")
    print(f"{'relevés dénoncés sur ' + str(apres['n_test']):<38}"
          f"{avant['signalements']:>8}{apres['signalements']:>8}")
    print(f"{'AUC (0.5 = hasard)':<38}{avant['auc']:>8.3f}{apres['auc']:>8.3f}")

    meilleur = meilleur_modele_honnete(df)
    print("\nMeilleur modèle honnête atteint (gradient boosting, témoignage résumé) :")
    print(f"  attrapés {meilleur['rappel'] * 100:.0f} / 100, "
          f"justes {meilleur['precision'] * 100:.0f} / 100, "
          f"{meilleur['signalements']} dénoncés, AUC {meilleur['auc']:.3f}")

    return apres, meilleur


def meilleur_modele_honnete(df):
    """Le meilleur qu'on obtienne sans jamais lire ce que le Bureau a écrit.

    Gradient boosting, qui attrape les effets non linéaires que la régression
    logistique manque, et témoignage résumé en 120 dimensions.
    """
    y = df["canular"].to_numpy().astype(int)
    i_train, i_test = train_test_split(
        np.arange(len(y)), test_size=0.25, stratify=y, random_state=GRAINE
    )
    encodeur = OneHotEncoder(
        handle_unknown="ignore", min_frequency=20, sparse_output=False
    )
    cats = df.select(COLS_CAT).fill_null("").to_numpy()
    nums = df.select(COLS_NUM + COLS_STYLE).to_numpy().astype(float)

    tfidf = TfidfVectorizer(max_features=20000, min_df=2)
    textes = df["temoignage"].to_list()
    resume = TruncatedSVD(n_components=120, random_state=GRAINE)
    texte_train = resume.fit_transform(tfidf.fit_transform([textes[i] for i in i_train]))
    texte_test = resume.transform(tfidf.transform([textes[i] for i in i_test]))

    modele = HistGradientBoostingClassifier(class_weight="balanced", random_state=GRAINE)
    modele.fit(
        np.hstack([encodeur.fit_transform(cats[i_train]), nums[i_train], texte_train]),
        y[i_train],
    )
    X_test = np.hstack([encodeur.transform(cats[i_test]), nums[i_test], texte_test])
    predit = modele.predict(X_test)

    return {
        "rappel": recall_score(y[i_test], predit),
        "precision": precision_score(y[i_test], predit, zero_division=0),
        "exactitude": accuracy_score(y[i_test], predit),
        "auc": roc_auc_score(y[i_test], modele.predict_proba(X_test)[:, 1]),
        "signalements": int(predit.sum()),
        "attrapes": int(((predit == 1) & (y[i_test] == 1)).sum()),
    }


COLS_TROUEES = ["country", "state", "duration_hours_min"]
COLS_MANQUE = ["manque_country", "manque_state", "manque_duration_hours_min"]


def est_vide(colonne):
    return pl.col(colonne).fill_null("").str.strip_chars() == ""


def ajouter_indicateurs(df):
    """Marque les trous avant de les boucher : ce sont deux choses différentes."""
    return df.with_columns(
        **{f"manque_{col}": est_vide(col).cast(pl.Int8) for col in COLS_TROUEES}
    )


TAILLE_TEXTE_DISCRIMINANT = 80


def grouper_evenements(df):
    """Numérote les événements : un numéro par apparition, pas par témoin.

    Deux relevés parlent du même événement s'ils partagent la ville, l'état et le
    jour, ou si leur témoignage est recopié mot pour mot. On n'applique le second
    critère qu'aux textes assez longs : « Fireball » écrit par douze personnes dans
    douze villes n'est pas un événement, c'est un mot banal.
    """
    parent = list(range(df.height))

    def racine(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def unir(membres):
        premier = racine(membres[0])
        for autre in membres[1:]:
            parent[racine(autre)] = premier

    indexe = df.with_row_index("i").with_columns(jour=pl.col("datetime").dt.date())
    meme_lieu_jour = indexe.group_by(["city", "state", "jour"]).agg(pl.col("i"))
    meme_texte = (
        indexe.filter(
            pl.col("comments").str.len_chars() > TAILLE_TEXTE_DISCRIMINANT
        )
        .group_by("comments")
        .agg(pl.col("i"))
    )
    for lot in (meme_lieu_jour, meme_texte):
        for membres in lot["i"].to_list():
            if len(membres) > 1:
                unir(membres)

    return np.array([racine(i) for i in range(df.height)])


def phase6_modele_du_stagiaire(honnete, meilleur):
    """Baseline « jamais un canular », à mettre en face du vrai modèle."""
    titre(6, "le modèle le plus bête du Bureau")

    vrai = honnete["y_test"]
    stagiaire = np.zeros_like(vrai)  # « ce n'est pas un canular », toujours
    canulars = int(vrai.sum())

    print("Système du stagiaire : répondre « ce n'est pas un canular », quoi qu'il arrive.")
    print(f"\n{'':<32}{'bonnes réponses':>16}{'attrapés':>14}{'relus pour rien':>17}")
    for nom, resultat in (
        ("le stagiaire", {"exactitude": accuracy_score(vrai, stagiaire),
                          "attrapes": 0, "signalements": 0}),
        ("mon modèle (régression)", honnete),
        ("mon meilleur modèle (boosting)", meilleur),
    ):
        attrapes = resultat["attrapes"]
        print(f"{nom:<32}{resultat['exactitude']:>15.2%}"
              f"{f'{attrapes} / {canulars}':>14}"
              f"{resultat['signalements'] - attrapes:>17}")

    print("\nLe stagiaire gagne sur les bonnes réponses parce que dire non à tout suffit")
    print(f"quand seulement {vrai.mean():.2%} des relevés sont des canulars : il se trompe")
    print(f"{canulars} fois, une par canular manqué, et n'accuse jamais personne à tort.")
    print("\nPour faire mieux que lui sur cette mesure, il faudrait accuser à tort moins")
    print("souvent qu'à raison, donc une précision au-dessus de 50 % : hors d'atteinte ici.")
    print("La mesure à présenter au Conseil est le nombre de canulars attrapés, avec le")
    print("nombre de dossiers à relire en face.")


def phase7_un_seul_evenement(df, avant):
    """Un événement ne doit pas avoir des témoins des deux côtés de la découpe."""
    titre(7, "plusieurs témoins, un seul événement")

    print("Deux relevés parlent du même événement s'ils partagent la ville, l'état et")
    print("le jour de l'observation, ou si leur témoignage est recopié mot pour mot.")

    groupes = grouper_evenements(df)
    tailles = Counter(groupes)
    plusieurs = {g: n for g, n in tailles.items() if n > 1}
    plus_gros = max(tailles, key=tailles.get)

    print(f"\nÉvénements signalés par plus d'un témoin : {len(plusieurs)}")
    print(f"Relevés concernés                        : {sum(plusieurs.values())}")
    print(f"Témoins du plus gros                     : {tailles[plus_gros]}")

    # Combien de relevés se retrouvaient des deux côtés avec la découpe d'hier ?
    y = df["canular"].to_numpy().astype(int)
    i_train, i_test = decouper(y)
    cote = np.zeros(len(y), dtype=int)
    cote[i_test] = 1
    a_cheval = sum(
        n
        for g, n in tailles.items()
        if n > 1 and len(set(cote[groupes == g])) > 1
    )
    print(f"Relevés à cheval dans la découpe d'hier  : {a_cheval}")

    doublons = (
        df.filter(pl.col("comments").str.strip_chars() != "")
        .group_by("comments")
        .len()
        .filter(pl.col("len") > 1)
    )
    print(f"\nTémoignages recopiés mot pour mot        : {int(doublons['len'].sum())} "
          f"relevés, {doublons.height} textes distincts")
    print(f"  dont textes de plus de {TAILLE_TEXTE_DISCRIMINANT} caractères, "
          f"traités comme un même événement : "
          f"{int(doublons.filter(pl.col('comments').str.len_chars() > TAILLE_TEXTE_DISCRIMINANT)['len'].sum())}")

    apres = entrainer(df, texte="temoignage", avec_delai=False, groupes=groupes)
    montrer_evenement(df, groupes, plus_gros, apres["indices_test"])

    print(f"\n{'':<38}{'avant':>8}{'après':>8}")
    print(f"{'sur 100 canulars réels, attrapés':<38}"
          f"{avant['rappel'] * 100:>8.0f}{apres['rappel'] * 100:>8.0f}")
    print(f"{'sur 100 signalés, vraiment canulars':<38}"
          f"{avant['precision'] * 100:>8.0f}{apres['precision'] * 100:>8.0f}")
    print(f"{'AUC':<38}{avant['auc']:>8.3f}{apres['auc']:>8.3f}")
    return apres, groupes


def montrer_evenement(df, groupes, numero, indices_test):
    """Affiche un événement entier, tous ses témoins du même côté de la découpe."""
    membres = np.flatnonzero(groupes == numero)
    dans_test = set(indices_test)
    cotes = {"test" if i in dans_test else "apprentissage" for i in membres}
    lignes = df[membres.tolist()]
    date = lignes["datetime"][0]
    print(f"\nÉvénement le plus signalé : {lignes['city'][0]} ({lignes['state'][0]}), "
          f"{date.date() if date else '?'} — {len(membres)} témoins, "
          f"tous du côté {' et '.join(cotes)}")
    for forme, texte in zip(lignes["shape"][:5], lignes["temoignage"][:5]):
        print(f"  [{forme or '—':<9}] {texte[:78].replace('&#44', ',')}")
    print(f"  … {len(membres) - 5} autres témoins" if len(membres) > 5 else "")


def phase8_ordre_du_temps(df, groupes, avant):
    """Apprendre sur le passé et être noté sur l'avenir, pas l'inverse."""
    titre(8, "l'ordre des choses")

    i_train, i_test, coupure = decoupe_temporelle(df, groupes)
    y = df["canular"].to_numpy().astype(int)

    print("Je coupe sur date_posted, la date à laquelle le Bureau a reçu le dossier,")
    print("et non sur celle de l'observation : c'est dans cet ordre-là que les dossiers")
    print("arrivent réellement, et c'est aussi dans cet ordre que le Bureau les annote.")
    print(f"\nDate de coupure : {coupure}")
    print(f"{'':<26}{'apprentissage':>15}{'test':>12}")
    print(f"{'relevés':<26}{len(i_train):>15}{len(i_test):>12}")
    print(f"{'canulars':<26}{int(y[i_train].sum()):>15}{int(y[i_test].sum()):>12}")
    print(f"{'proportion de canulars':<26}{y[i_train].mean():>14.2%}{y[i_test].mean():>12.2%}")

    # Les deux proportions diffèrent : d'où ça vient ?
    par_an = (
        df.with_columns(an=pl.col("date_posted").dt.year())
        .group_by("an")
        .agg(
            taux=pl.col("canular").mean(),
            annotes=(pl.col("comments").str.contains(r"\(\(")).mean(),
        )
        .sort("an")
    )
    print("\nTaux de canulars et d'annotation du Bureau, par année de publication :")
    for ligne in par_an.iter_rows(named=True):
        print(f"  {ligne['an']}  canulars {ligne['taux']:>6.2%}   "
              f"dossiers annotés {ligne['annotes']:>6.2%}")

    apres = entrainer(
        df, texte="temoignage", avec_delai=False, decoupe=(i_train, i_test)
    )
    print(f"\n{'':<38}{'avant':>8}{'après':>8}")
    print(f"{'sur 100 canulars réels, attrapés':<38}"
          f"{avant['rappel'] * 100:>8.0f}{apres['rappel'] * 100:>8.0f}")
    print(f"{'sur 100 signalés, vraiment canulars':<38}"
          f"{avant['precision'] * 100:>8.0f}{apres['precision'] * 100:>8.0f}")
    print(f"{'AUC':<38}{avant['auc']:>8.3f}{apres['auc']:>8.3f}")
    return apres, (i_train, i_test, coupure)


UNITES = {
    "sec": 1.0, "min": 60.0, "hour": 3600.0, "hr": 3600.0,
    "day": 86400.0, "week": 604800.0, "month": 2592000.0,
}
UN_JOUR = 86400


MOTS_NOMBRES = {
    "one": 1.0, "two": 2.0, "three": 3.0, "four": 4.0, "five": 5.0, "six": 6.0,
    "seven": 7.0, "eight": 8.0, "nine": 9.0, "ten": 10.0, "fifteen": 15.0,
    "twenty": 20.0, "thirty": 30.0, "forty": 40.0, "sixty": 60.0,
    "half": 0.5, "couple": 2.0,
}


def duree_ecrite_par_le_temoin():
    """Relit « 5 minutes », « 1-2 hrs », « one minute » : ce que le témoin a tapé."""
    texte = pl.col("duration_hours_min").fill_null("").str.to_lowercase()
    chiffres = (
        texte.str.extract(r"(\d+(?:[.,]\d+)?)", 1)
        .str.replace(",", ".")
        .cast(pl.Float64, strict=False)
    )
    # « one minute », « five minutes » : le témoin écrit aussi les nombres en lettres.
    en_lettres = texte.str.extract(
        r"\b(" + "|".join(MOTS_NOMBRES) + r")\b", 1
    ).replace_strict(MOTS_NOMBRES, default=None, return_dtype=pl.Float64)
    nombre = pl.coalesce(chiffres, en_lettres)
    unite = texte.str.extract(r"(seconds?|sec|minutes?|min|hours?|hour|hrs?|days?|weeks?|months?)", 1)
    facteur = (
        pl.when(unite.str.starts_with("sec")).then(pl.lit(1.0))
        .when(unite.str.starts_with("min")).then(pl.lit(60.0))
        .when(unite.str.starts_with("h")).then(pl.lit(3600.0))
        .when(unite.str.starts_with("day")).then(pl.lit(86400.0))
        .when(unite.str.starts_with("week")).then(pl.lit(604800.0))
        .when(unite.str.starts_with("month")).then(pl.lit(2592000.0))
        .otherwise(None)
    )
    return nombre * facteur


def phase11_duree(df):
    """Deux colonnes de durée qui ne sont pas d'accord : en tirer une seule, fiable."""
    titre(11, "combien de temps ça a duré")
    lignes_avant = df.height

    df = df.with_columns(duree_ecrite=duree_ecrite_par_le_temoin())
    # La colonne « propre » a été fabriquée à partir de la sale, et parfois ratée :
    # quand elle annonce 0 ou rien, on retombe sur ce que le témoin avait écrit.
    df = df.with_columns(
        duree=pl.when(pl.col("duration_seconds") > 0)
        .then(pl.col("duration_seconds"))
        .otherwise(pl.col("duree_ecrite"))
    )

    lisibles_deux = pl.col("duration_seconds").is_not_null() & pl.col(
        "duree_ecrite"
    ).is_not_null() & (pl.col("duration_seconds") > 0)
    ecart = (pl.col("duration_seconds") / pl.col("duree_ecrite")).abs()
    contradiction = lisibles_deux & ((ecart > 2) | (ecart < 0.5))

    perdue = (
        (pl.col("duration_seconds").is_null() | (pl.col("duration_seconds") == 0))
        & pl.col("duree_ecrite").is_not_null()
    )
    compte = lambda expr: int(df.select(expr).to_series().sum())  # noqa: E731

    ecrite_vide = est_vide("duration_hours_min")
    print(f"Durées écrites à la main relues avec succès : "
          f"{compte(pl.col('duree_ecrite').is_not_null())} "
          f"({compte(ecrite_vide)} cases vides, "
          f"{compte(~ecrite_vide & pl.col('duree_ecrite').is_null())} illisibles)")
    print(f"Relevés dont la durée reste inutilisable : {compte(pl.col('duree').is_null())}")
    print(f"  (avant récupération du texte du témoin : "
          f"{compte(pl.col('duration_seconds').is_null() | (pl.col('duration_seconds') == 0))})")
    print(f"Relevés où les deux colonnes se contredisent : {compte(contradiction)}")
    print(f"Durée médiane : {df['duree'].median():.0f} secondes")
    print(f"Relevés annonçant plus d'une journée : {compte(pl.col('duree') > UN_JOUR)}")

    print("\nDeux natures d'aberration :")
    print(f"  durée perdue par la transmission (0 alors que le témoin avait écrit) : "
          f"{compte(perdue)}")
    print(f"  durée physiquement invraisemblable (plus d'une journée)              : "
          f"{compte(pl.col('duree') > UN_JOUR)}")

    print("\nLes trois durées les plus longues :")
    plus_longues = df.sort("duree", descending=True, nulls_last=True).head(3)
    for ligne in plus_longues.iter_rows(named=True):
        jours = ligne["duree"] / UN_JOUR
        print(f"  {ligne['duree']:>12.0f} s ({jours:>7.0f} jours)  "
              f"écrit : « {(ligne['duration_hours_min'] or '')[:28]} »")
    print("  Je les garde : ce sont des relevés valides par ailleurs, et les supprimer")
    print("  changerait le nombre de lignes. Je marque simplement l'invraisemblance,")
    print("  et le modèle lit le logarithme de la durée, insensible à ces extrêmes.")

    exemple = df.filter(contradiction).sort("duree_ecrite", descending=True).head(1)
    if exemple.height:
        ligne = exemple.row(0, named=True)
        print(f"\nUn relevé où les deux colonnes racontent deux histoires :")
        print(f"  duration_seconds   : {ligne['duration_seconds']:.0f}")
        print(f"  duration_hours_min : « {ligne['duration_hours_min']} » "
              f"→ {ligne['duree_ecrite']:.0f} s")
        print(f"  témoignage         : {(ligne['comments'] or '')[:70]}")

    df = df.with_columns(
        duree_log=(pl.col("duree").fill_null(0) + 1).log(),
        duree_invraisemblable=(pl.col("duree") > UN_JOUR).cast(pl.Int8),
    )
    print(f"\nLignes : {lignes_avant} en entrée, {df.height} en sortie.")
    return df


class Chaine:
    """Toute la chaîne, du relevé brut à la réponse.

    Rien ne s'apprend avant `apprendre` : le vocabulaire, la liste des catégories,
    les médianes et les échelles sortent tous de la partie apprentissage seule. Un
    relevé neuf entre par `repondre` et ressort avec un verdict, sans étape à
    retaper à la main.
    """

    def __init__(self, colonne_texte="temoignage", cols_cat=None, cols_num=None):
        self.colonne_texte = colonne_texte
        self.cols_cat = cols_cat or COLS_CAT
        self.cols_num = cols_num or COLS_NUM

    def apprendre(self, df_brut, y):
        df = preparer(df_brut)
        self.tfidf = TfidfVectorizer(max_features=20000, min_df=2)
        texte = self.tfidf.fit_transform(df[self.colonne_texte].to_list())

        self.encodeur = OneHotEncoder(
            handle_unknown="ignore", min_frequency=VILLE_RARE
        )
        categories = self.encodeur.fit_transform(self._categories(df))

        nombres = self._nombres(df)
        self.mediane = np.nanmedian(nombres, axis=0)
        self.echelle = StandardScaler()
        echelonnes = self.echelle.fit_transform(self._boucher(nombres))

        self.modele = LogisticRegression(solver="liblinear", class_weight="balanced")
        self.modele.fit(
            hstack([texte, categories, csr_matrix(echelonnes)]).tocsr(), y
        )
        return self

    def _categories(self, df):
        return df.select(self.cols_cat).fill_null("").to_numpy()

    def _nombres(self, df):
        return (
            df.select(self.cols_num + COLS_STYLE + COLS_MANQUE).to_numpy().astype(float)
        )

    def _boucher(self, nombres):
        return np.where(np.isnan(nombres), self.mediane, nombres)

    def _transformer(self, df_brut):
        df = preparer(df_brut)
        return hstack(
            [
                self.tfidf.transform(df[self.colonne_texte].to_list()),
                self.encodeur.transform(self._categories(df)),
                csr_matrix(self.echelle.transform(self._boucher(self._nombres(df)))),
            ]
        ).tocsr()

    def repondre(self, df_brut):
        """Un relevé brut entre, un verdict sort."""
        X = self._transformer(df_brut)
        return self.modele.predict(X), self.modele.decision_function(X)


RELEVE_INVENTE = {
    "datetime": "6/15/2015 23:30",
    "city": "nulle part",
    "state": "tx",
    "country": "us",
    "shape": "triangle",
    "duration_seconds": "300",
    "duration_hours_min": "5 minutes",
    "comments": "Three silent lights in a triangle formation moving slowly north.",
    "date_posted": "6/20/2015",
    "latitude": "31.9686",
    "longitude": "-99.9018",
}


def phase10_chaine_de_traitement(df_brut, decoupe, avant):
    """Rien d'appris avant la découpe, et un relevé neuf traverse tout d'un coup."""
    titre(10, "la chaîne de traitement du Bureau")

    i_train, i_test = decoupe
    y = df_brut["canular"].to_numpy().astype(int)
    print("Proportion de canulars dans chaque partie :")
    print(f"  apprentissage : {y[i_train].mean():.2%} sur {len(i_train)} relevés")
    print(f"  test          : {y[i_test].mean():.2%} sur {len(i_test)} relevés")

    chaine = Chaine().apprendre(df_brut[i_train.tolist()], y[i_train])
    predit, _ = chaine.repondre(df_brut[i_test.tolist()])

    print("\nUn relevé inventé à la main traverse la chaîne :")
    for champ, valeur in list(RELEVE_INVENTE.items())[:4]:
        print(f"  {champ:<18} {valeur}")
    print("  …")
    verdict, score = chaine.repondre(pl.DataFrame([RELEVE_INVENTE]))
    print(f"  → verdict : {'CANULAR' if verdict[0] else 'pas un canular'} "
          f"(score {score[0]:+.2f})")

    apres = {
        "rappel": recall_score(y[i_test], predit),
        "precision": precision_score(y[i_test], predit, zero_division=0),
        "auc": roc_auc_score(y[i_test], chaine.repondre(df_brut[i_test.tolist()])[1]),
    }
    print(f"\n{'':<38}{'avant':>8}{'après':>8}")
    print(f"{'sur 100 canulars réels, attrapés':<38}"
          f"{avant['rappel'] * 100:>8.0f}{apres['rappel'] * 100:>8.0f}")
    print(f"{'sur 100 signalés, vraiment canulars':<38}"
          f"{avant['precision'] * 100:>8.0f}{apres['precision'] * 100:>8.0f}")
    print(f"{'AUC':<38}{avant['auc']:>8.3f}{apres['auc']:>8.3f}")
    return apres, chaine


def distance_horaire(a, b):
    """Distance entre deux heures une fois placées sur le cercle des 24 h."""
    angle = lambda h: h * 2 * np.pi / 24  # noqa: E731
    return float(
        np.hypot(np.sin(angle(a)) - np.sin(angle(b)), np.cos(angle(a)) - np.cos(angle(b)))
    )


def phase12_ville_et_heure(df_brut, decoupe, avant):
    """Deux informations riches, deux façons de se planter."""
    titre(12, "la ville et l'heure")

    i_train, i_test = decoupe
    y = df_brut["canular"].to_numpy().astype(int)

    villes = df_brut["city"].value_counts(sort=True)
    print(f"Villes distinctes                          : {villes.height}")
    print(f"Villes qui n'apparaissent qu'une seule fois : "
          f"{villes.filter(pl.col('count') == 1).height}")
    print(f"\nRègle : je garde les villes vues au moins {VILLE_RARE} fois dans la partie")
    print("apprentissage, et je verse toutes les autres dans un même sac « ville rare ».")

    print(f"\nL'heure sur un cercle plutôt que sur une règle graduée :")
    print(f"  distance entre 23 h et 0 h  : {distance_horaire(23, 0):.3f}")
    print(f"  distance entre 23 h et 20 h : {distance_horaire(23, 20):.3f}")

    formes = preparer(df_brut)["shape"].n_unique()
    print(f"\nFormes après avoir fondu « changed » dans « changing » et « round »")
    print(f"dans « circle » : {formes}, dont les plus rares seront regroupées ensuite.")

    chaine = Chaine(
        cols_cat=COLS_CAT_FINAL, cols_num=COLS_NUM_FINAL
    ).apprendre(df_brut[i_train.tolist()], y[i_train])
    rang_ville = COLS_CAT_FINAL.index("city")
    noms = chaine.encodeur.get_feature_names_out()
    colonnes_ville = sum(1 for n in noms if n.startswith(f"x{rang_ville}_"))
    largeur = chaine._transformer(df_brut[:1]).shape[1]
    villes_train = df_brut[i_train.tolist()]["city"].n_unique()

    print(f"\nLargeur du tableau donné au modèle :")
    print(f"  sans la ville                        : {largeur - colonnes_ville:>6}")
    print(f"  avec une colonne par ville (naïf)    : "
          f"{largeur - colonnes_ville + villes_train:>6}")
    print(f"  avec la règle                        : {largeur:>6}"
          f"   ({colonnes_ville} colonnes de ville)")

    predit, scores = chaine.repondre(df_brut[i_test.tolist()])
    apres = {
        "rappel": recall_score(y[i_test], predit),
        "precision": precision_score(y[i_test], predit, zero_division=0),
        "auc": roc_auc_score(y[i_test], scores),
    }
    print(f"\n{'':<38}{'avant':>8}{'après':>8}")
    print(f"{'sur 100 canulars réels, attrapés':<38}"
          f"{avant['rappel'] * 100:>8.0f}{apres['rappel'] * 100:>8.0f}")
    print(f"{'sur 100 signalés, vraiment canulars':<38}"
          f"{avant['precision'] * 100:>8.0f}{apres['precision'] * 100:>8.0f}")
    print(f"{'AUC':<38}{avant['auc']:>8.3f}{apres['auc']:>8.3f}")
    print("\nAucun de mes encodages ne se sert de la cible : ce sont des comptages de")
    print("fréquence, appris sur la partie apprentissage seule, jamais sur le test.")
    return apres


def phase9_cases_vides(df, decoupe, avant):
    """Mesurer ce que dit un trou avant de le boucher."""
    titre(9, "les cases vides")

    texte = ["city", "state", "country", "shape", "duration_hours_min", "comments"]
    combien = {col: int(df.select(est_vide(col)).to_series().sum()) for col in texte}
    trois = sorted(combien, key=combien.get, reverse=True)[:3]
    print(f"Les trois colonnes les plus trouées : {', '.join(trois)}")

    print(f"\n{'colonne':<20}{'vides':>8}{'canulars si vide':>19}{'si rempli':>12}")
    for col in trois:
        manque = df.select(est_vide(col)).to_series()
        avec = df.filter(manque)["canular"].mean()
        sans = df.filter(~manque)["canular"].mean()
        print(f"{col:<20}{combien[col]:>8}{avec:>18.2%}{sans:>12.2%}")

    df = ajouter_indicateurs(df)
    print("\nTraitement : je garde les vides comme une catégorie à part entière et")
    print("j'ajoute une colonne « cette case était vide » pour chacune des trois.")
    print("Le trou est bouché, sa trace est conservée, le modèle peut encore s'en servir.")

    apres = entrainer(df, texte="temoignage", avec_delai=False, decoupe=decoupe)
    print(f"\n{'':<38}{'avant':>8}{'après':>8}")
    print(f"{'sur 100 canulars réels, attrapés':<38}"
          f"{avant['rappel'] * 100:>8.0f}{apres['rappel'] * 100:>8.0f}")
    print(f"{'sur 100 signalés, vraiment canulars':<38}"
          f"{avant['precision'] * 100:>8.0f}{apres['precision'] * 100:>8.0f}")
    print(f"{'AUC':<38}{avant['auc']:>8.3f}{apres['auc']:>8.3f}")
    return apres, df


def main():
    recuperer_la_transmission()
    brut = phase1_ouvrir_la_caisse()
    df = phase2_typer(brut)
    df = phase3_etiqueter_les_canulars(df)
    df = ajouter_temoignage(ajouter_delai(df))
    avant = phase4_premier_verdict(df)
    honnete, meilleur = phase5_fuite_temporelle(df, avant)
    phase6_modele_du_stagiaire(honnete, meilleur)
    par_evenement, groupes = phase7_un_seul_evenement(df, honnete)
    dans_le_temps, (i_train, i_test, _) = phase8_ordre_du_temps(df, groupes, par_evenement)
    sans_trous, _ = phase9_cases_vides(df, (i_train, i_test), dans_le_temps)
    avec_etiquette = brut.with_columns(canular=df["canular"])
    en_chaine, _ = phase10_chaine_de_traitement(
        avec_etiquette, (i_train, i_test), sans_trous
    )
    phase11_duree(df)
    phase12_ville_et_heure(avec_etiquette, (i_train, i_test), en_chaine)
    print("\nFin de l'analyse. Les chiffres sont à reporter dans RAPPORT.md.")


if __name__ == "__main__":
    main()
