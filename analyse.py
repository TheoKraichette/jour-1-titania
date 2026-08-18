"""Bureau d'Analyse Terrestre — relevés de la sonde Klaxo-3.

Télécharge la transmission et rejoue toutes les phases d'une traite.

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
from sklearn.isotonic import IsotonicRegression
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
    print(f"Mis à part                 : {len(rejets)} "
          f"({len(rejets) / total:.2%} du fichier)")
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
        # trié : `unique` ne garantit pas l'ordre, et deux lancements doivent
        # afficher les mêmes exemples.
        fautives = brut.filter(resiste).unique().sort().head(4).to_list()
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
    # Trié aussi sur le numéro d'événement : beaucoup de dossiers sont publiés le
    # même jour, et sans second critère la coupure ne tombe pas deux fois au même
    # endroit.
    par_groupe = (
        pl.DataFrame({"groupe": groupes, "publie": df["date_posted"]})
        .group_by("groupe", maintain_order=True)
        .agg(pl.col("publie").min().alias("publie"), pl.len().alias("taille"))
        .sort(["publie", "groupe"])
    )
    cumul = par_groupe["taille"].cum_sum()
    limite = int(df.height * (1 - part_test))
    au_train = par_groupe.filter(cumul <= limite)["groupe"].to_list()
    date_coupure = par_groupe.filter(cumul > limite)["publie"][0]

    marque = np.zeros(int(groupes.max()) + 1, dtype=bool)
    marque[au_train] = True
    dans_train = marque[groupes]
    return np.flatnonzero(dans_train), np.flatnonzero(~dans_train), date_coupure


def entrainer(df, texte, avec_delai, C, groupes=None, decoupe=None):
    """Entraîne un modèle et l'évalue sur un quart des relevés, mis de côté d'avance.

    `texte` désigne la colonne de texte donnée au modèle, ou None pour n'en donner aucune.
    `C` est la force de régularisation, réglée une fois pour toutes avant la phase 4.
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
    # random_state : liblinear mélange les relevés avant de descendre, et sans graine
    # deux lancements ne rendent pas exactement les mêmes chiffres.
    modele = LogisticRegression(
        solver="liblinear", class_weight="balanced", C=C, random_state=GRAINE
    )
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


GRILLE_C = [0.003, 0.01, 0.03, 0.1, 0.3, 1.0]


def regler_regularisation(df):
    """Choisit la force de régularisation avant tout le reste.

    Le choix se fait sur une validation découpée dans l'apprentissage : le test
    n'y entre pas, et la valeur retenue ne bouge plus ensuite. Les comparaisons
    avant/après des phases suivantes portent ainsi sur ce que je corrige, pas sur
    un réglage qui aurait changé en route.
    """
    y = df["canular"].to_numpy().astype(int)
    i_train, _ = decouper(y)
    i_a, i_b = train_test_split(
        i_train, test_size=0.25, stratify=y[i_train], random_state=GRAINE
    )
    print("Force de régularisation, réglée sur une validation prise dans")
    print(f"l'apprentissage ({len(i_a)} relevés pour apprendre, {len(i_b)} pour juger) :")
    print(f"{'C':>8}{'AUC':>10}")

    essais = []
    for C in GRILLE_C:
        auc = entrainer(df, texte="temoignage", avec_delai=False, C=C,
                        decoupe=(i_a, i_b))["auc"]
        essais.append((C, auc))
        print(f"{C:>8}{auc:>10.3f}")

    retenu = max(essais, key=lambda e: e[1])[0]
    print(f"Retenu : C = {retenu} (la valeur par défaut des bibliothèques est 1).")
    return retenu


def phase4_premier_verdict(df, C):
    """Un modèle, évalué sur des relevés jamais vus : rappel et précision."""
    titre(4, "le premier verdict")

    resultat = entrainer(df, texte="comments", avec_delai=True, C=C)
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


def phase5_fuite_temporelle(df, avant, C):
    """Retirer les colonnes remplies après coup, réentraîner, comparer."""
    titre(5, "le Conseil ne vous croit pas")

    print(f"{'ce que le modèle lit':<34} {'qui écrit':<22} {'à quel moment':<16} savait ?")
    for colonne, qui, quand, savait in PROVENANCE:
        print(f"{colonne:<34} {qui:<22} {quand:<16} {'OUI' if savait else 'non'}")

    sortent = [colonne for colonne, _, _, savait in PROVENANCE if savait]
    print(f"\nSortent du modèle : {', '.join(sortent)}")

    apres = entrainer(df, texte="temoignage", avec_delai=False, C=C)

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

    # 64 paliers au lieu de 255 : trois fois plus rapide, et le résultat ne perd rien.
    modele = HistGradientBoostingClassifier(
        class_weight="balanced", random_state=GRAINE, max_bins=64
    )
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

    # maintain_order : sans lui, l'ordre des groupes varie d'un lancement à l'autre
    # et les numéros d'événement avec lui.
    indexe = df.with_row_index("i").with_columns(jour=pl.col("datetime").dt.date())
    meme_lieu_jour = indexe.group_by(
        ["city", "state", "jour"], maintain_order=True
    ).agg(pl.col("i"))
    meme_texte = (
        indexe.filter(
            pl.col("comments").str.len_chars() > TAILLE_TEXTE_DISCRIMINANT
        )
        .group_by("comments", maintain_order=True)
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


def phase7_un_seul_evenement(df, avant, C):
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
    a_cheval = (
        pl.DataFrame({"groupe": groupes, "cote": cote})
        .group_by("groupe")
        .agg(pl.len().alias("temoins"), pl.col("cote").n_unique().alias("cotes"))
        .filter(pl.col("cotes") > 1)["temoins"]
        .sum()
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

    apres = entrainer(df, texte="temoignage", avec_delai=False, C=C, groupes=groupes)
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


def phase8_ordre_du_temps(df, groupes, avant, C):
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
            releves=pl.len(),
            taux=pl.col("canular").mean(),
            annotes=(pl.col("comments").str.contains(r"\(\(")).mean(),
        )
        .sort("an")
    )
    print("\nTaux de canulars et d'annotation du Bureau, par année de publication :")
    for ligne in par_an.iter_rows(named=True):
        print(f"  {ligne['an']}  {ligne['releves']:>6} relevés   "
              f"canulars {ligne['taux']:>6.2%}   "
              f"dossiers annotés {ligne['annotes']:>6.2%}")

    apres = entrainer(
        df, texte="temoignage", avec_delai=False, C=C, decoupe=(i_train, i_test)
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

    illisibles = (
        df.filter(~ecrite_vide & pl.col("duree_ecrite").is_null())["duration_hours_min"]
        .str.to_lowercase()
        .str.strip_chars()
        .value_counts(sort=True)
        .head(4)
    )
    print("  ce que contiennent les illisibles : " + ", ".join(
        f"« {l['duration_hours_min']} » ({l['count']})" for l in illisibles.iter_rows(named=True)
    ))

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

    def __init__(self, colonne_texte="temoignage", cols_cat=None, cols_num=None,
                 C=1.0):
        self.colonne_texte = colonne_texte
        self.C = C
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

        self.modele = LogisticRegression(
            solver="liblinear", class_weight="balanced", C=self.C, random_state=GRAINE
        )
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

    def probabilites(self, df_brut):
        """La chance que le relevé soit un canular, entre 0 et 1."""
        return self.modele.predict_proba(self._transformer(df_brut))[:, 1]

    def noms_des_colonnes(self):
        """Le nom lisible de chaque colonne du tableau donné au modèle."""
        mots = [f"mot « {m} »" for m in self.tfidf.get_feature_names_out()]
        categories = []
        for nom in self.encodeur.get_feature_names_out():
            rang, valeur = nom.split("_", 1)
            categories.append(f"{self.cols_cat[int(rang[1:])]} = {valeur}")
        return mots + categories + self.cols_num + COLS_STYLE + COLS_MANQUE

    def ce_qui_a_pese(self, ligne_brute, combien=6):
        """Ce qui a poussé la décision pour un relevé, dans un sens ou dans l'autre.

        Le modèle est une somme : chaque colonne apporte sa valeur multipliée par
        son coefficient. Il suffit de regarder les termes les plus gros.
        """
        poids = (
            self._transformer(ligne_brute).toarray()[0] * self.modele.coef_[0]
        )
        noms = self.noms_des_colonnes()
        return [
            (noms[i], float(poids[i]))
            for i in np.argsort(-np.abs(poids))[:combien]
            if poids[i] != 0
        ]


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


def phase10_chaine_de_traitement(df_brut, decoupe, avant, C):
    """Rien d'appris avant la découpe, et un relevé neuf traverse tout d'un coup."""
    titre(10, "la chaîne de traitement du Bureau")

    i_train, i_test = decoupe
    y = df_brut["canular"].to_numpy().astype(int)
    print("Proportion de canulars dans chaque partie :")
    print(f"  apprentissage : {y[i_train].mean():.2%} sur {len(i_train)} relevés")
    print(f"  test          : {y[i_test].mean():.2%} sur {len(i_test)} relevés")

    chaine = Chaine(C=C).apprendre(df_brut[i_train.tolist()], y[i_train])
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


def phase12_ville_et_heure(df_brut, decoupe, avant, C):
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

    brutes = (
        df_brut["shape"].fill_null("").str.to_lowercase().str.strip_chars()
    )
    formes = preparer(df_brut)["shape"].n_unique()
    print(f"\nFormes : {brutes.n_unique()} au départ, dont {(brutes == '').sum()} relevés")
    print("sans forme renseignée. Après avoir fondu « changed » dans « changing » et")
    print(f"« round » dans « circle » : {formes}, la case vide comprise.")

    chaine = Chaine(
        cols_cat=COLS_CAT_FINAL, cols_num=COLS_NUM_FINAL, C=C
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
    return apres, chaine


# La grille votée par le Conseil, en crédits.
COUT_CANULAR_RATE = 30
COUT_FAUSSE_ALERTE = 2


def facture(y, probas, seuil):
    """Ce que coûte au Bureau une frontière posée à `seuil`."""
    denonce = probas >= seuil
    rates = int((~denonce & (y == 1)).sum())
    fausses = int((denonce & (y == 0)).sum())
    return {
        "rates": rates,
        "fausses": fausses,
        "attrapes": int((denonce & (y == 1)).sum()),
        "credits": rates * COUT_CANULAR_RATE + fausses * COUT_FAUSSE_ALERTE,
    }


def frontiere_la_moins_chere(y, probas):
    """La frontière qui coûte le moins cher, et sa facture.

    La facture ne change qu'aux probabilités réellement observées : les parcourir
    dans l'ordre décroissant suffit à toutes les essayer. Les ex æquo partent
    ensemble, puisqu'aucune frontière ne saurait les séparer.
    """
    ordre = np.argsort(-probas, kind="stable")
    attrapes = np.cumsum(y[ordre])
    denonces = np.arange(1, len(y) + 1)
    fin_de_paquet = np.r_[probas[ordre][1:] != probas[ordre][:-1], True]

    seuils = np.r_[probas[ordre][fin_de_paquet], 1.0 + 1e-9]
    pris = np.r_[attrapes[fin_de_paquet], 0]
    accuses = np.r_[denonces[fin_de_paquet], 0]
    couts = (y.sum() - pris) * COUT_CANULAR_RATE + (accuses - pris) * COUT_FAUSSE_ALERTE

    i = int(np.argmin(couts))
    return float(seuils[i]), int(couts[i])


def courbe_de_la_facture(y, probas, retenue):
    """La facture en fonction de la frontière, du premier au dernier crédit."""
    grille = np.linspace(0, 1, 501)
    couts = [facture(y, probas, s)["credits"] for s in grille]
    os.makedirs("figures", exist_ok=True)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.plot(grille, couts, color="#1f4e79", linewidth=1.6)
    ax.axvline(0.5, color="#888", linestyle="--",
               label=f"frontière par défaut (0,5) : {facture(y, probas, 0.5)['credits']} cr.")
    ax.axvline(retenue, color="#c1440e",
               label=f"frontière retenue ({retenue:.3f}) : "
                     f"{facture(y, probas, retenue)['credits']} cr.")
    ax.set_xlabel("frontière")
    ax.set_ylabel("facture (crédits)")
    ax.set_title("Ce que coûte au Bureau chaque frontière possible")
    ax.legend()
    fig.savefig("figures/facture.png", dpi=130, bbox_inches="tight")
    plt.close(fig)
    print("\nCourbe : figures/facture.png")


def phase13_facture_du_bureau(chaine, df_brut, decoupe, df, groupes, C):
    """Un score n'est pas une décision : reste à poser la frontière, et à la payer."""
    titre(13, "la facture du Bureau")

    i_train, i_test = decoupe
    y = df_brut["canular"].to_numpy().astype(int)
    probas = chaine.probabilites(df_brut[i_test.tolist()])
    y_test = y[i_test]

    print(f"Grille du Conseil : un canular laissé passer coûte {COUT_CANULAR_RATE} "
          f"crédits,\nune fausse alerte {COUT_FAUSSE_ALERTE}. Le reste est gratuit.")

    bascule = COUT_FAUSSE_ALERTE / (COUT_CANULAR_RATE + COUT_FAUSSE_ALERTE)
    print(f"Dénoncer un relevé de plus rapporte donc s'il a plus de {bascule:.2%} de")
    print("chances d'être un canular : en dessous, la fausse alerte coûte plus cher.")

    retenue, _ = frontiere_la_moins_chere(y_test, probas)
    a_montrer = sorted({0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, retenue})

    print(f"\n{'frontière':>10}{'dénoncés':>10}{'attrapés':>10}"
          f"{'ratés':>8}{'fausses alertes':>17}{'facture':>10}")
    for seuil in a_montrer:
        f = facture(y_test, probas, seuil)
        marque = "  <-" if seuil == retenue else ""
        print(f"{seuil:>10.3f}{f['attrapes'] + f['fausses']:>10}{f['attrapes']:>10}"
              f"{f['rates']:>8}{f['fausses']:>17}{f['credits']:>10}{marque}")
    silence = int(y_test.sum()) * COUT_CANULAR_RATE
    print(f"{'silence':>10}{0:>10}{0:>10}{int(y_test.sum()):>8}{0:>17}{silence:>10}")

    defaut = facture(y_test, probas, 0.5)
    retenu = facture(y_test, probas, retenue)
    print(f"\nFacture à 0,5 (la frontière que personne n'a choisie) : "
          f"{defaut['credits']} crédits")
    print(f"  dont {defaut['fausses']} fausses alertes "
          f"({defaut['fausses'] * COUT_FAUSSE_ALERTE} crédits) et {defaut['rates']} "
          f"canulars manqués ({defaut['rates'] * COUT_CANULAR_RATE} crédits)")
    print(f"Facture à {retenue:.3f} (la mienne)                       : "
          f"{retenu['credits']} crédits")
    print(f"Écart                                              : "
          f"{defaut['credits'] - retenu['credits']} crédits économisés")
    print(f"Pour mémoire, ne dénoncer personne en coûterait     : {silence} crédits")

    courbe_de_la_facture(y_test, probas, retenue)

    # Contrôle : la frontière choisie sur le test est la meilleure par construction.
    # Celle-ci est réglée sans jamais le regarder, sur le quart le plus récent de
    # l'apprentissage, puis appliquée au test comme au premier jour.
    veille = repetition_generale(df, df_brut, i_train, groupes, y, C)
    seuil_hors_test, _ = frontiere_la_moins_chere(veille["y_val"], veille["probas_val"])
    aveugle = facture(y_test, probas, seuil_hors_test)
    print(f"\nContrôle — frontière réglée sans regarder le test : {seuil_hors_test:.3f}, "
          f"soit {aveugle['credits']} crédits sur le test\n"
          f"(l'optimum du test en coûte {retenu['credits']}).")

    return {"retenue": retenue, "facture_retenue": retenu["credits"],
            "facture_defaut": defaut["credits"], "probas": probas, "y_test": y_test,
            "veille": veille, "silence": silence}


def repetition_generale(df, df_brut, i_train, groupes, y, C):
    """Une répétition de l'exercice à l'intérieur de l'apprentissage.

    Même découpe dans le temps, mais un cran plus tôt : on apprend sur les trois
    premiers quarts de l'apprentissage et on se juge sur le dernier. Tout ce qui a
    besoin d'être réglé — la frontière, la courbe de correction des probabilités —
    se règle ici, où le test n'existe pas encore.
    """
    i_a, i_b, _ = decoupe_temporelle(df[i_train.tolist()], groupes[i_train], 0.25)
    reserve = df_brut[i_train.tolist()]
    chaine = Chaine(cols_cat=COLS_CAT_FINAL, cols_num=COLS_NUM_FINAL, C=C).apprendre(
        reserve[i_a.tolist()], y[i_train][i_a]
    )
    return {
        "chaine": chaine,
        "probas_val": chaine.probabilites(reserve[i_b.tolist()]),
        "y_val": y[i_train][i_b],
    }


def tranches_de_probabilite(y, probas, combien=10):
    """Range les relevés par probabilité annoncée et les coupe en paquets égaux.

    Des tranches de largeur fixe (0-0,1, 0,1-0,2…) ne tiennent pas ici : une fois
    les probabilités corrigées, elles tomberaient toutes dans la première. Des
    paquets de même effectif restent lisibles avant comme après.
    """
    ordre = np.argsort(probas, kind="stable")
    lignes = []
    for paquet in np.array_split(ordre, combien):
        lignes.append({
            "de": float(probas[paquet].min()),
            "a": float(probas[paquet].max()),
            "n": len(paquet),
            "annonce": float(probas[paquet].mean()),
            "observe": float(y[paquet].mean()),
        })
    return lignes


def ecart_de_calibration(lignes):
    """L'écart moyen entre ce qui est annoncé et ce qui se produit, pondéré."""
    total = sum(l["n"] for l in lignes)
    return sum(l["n"] * abs(l["annonce"] - l["observe"]) for l in lignes) / total


def montrer_tranches(intitule, lignes):
    print(f"\n{intitule}")
    print(f"{'probabilité annoncée':>22}{'relevés':>9}{'annoncé':>10}"
          f"{'observé':>10}{'écart':>9}")
    for l in lignes:
        print(f"{l['de']:>10.3f} – {l['a']:<9.3f}{l['n']:>9}{l['annonce']:>10.1%}"
              f"{l['observe']:>10.1%}{l['annonce'] - l['observe']:>+9.1%}")
    print(f"{'écart moyen':>22}{'':>9}{'':>10}{'':>10}"
          f"{ecart_de_calibration(lignes):>9.1%}")


def phase14_promesse_a_80(resultat13):
    """Bien trier et mal chiffrer sont deux choses différentes."""
    titre(14, "une promesse à 80 %")

    y_test, probas, veille = (
        resultat13["y_test"], resultat13["probas"], resultat13["veille"]
    )

    avant = tranches_de_probabilite(y_test, probas)
    montrer_tranches("Ce que le système annonce, en face de ce qui s'est produit :", avant)

    # La question de la conseillère, mot pour mot.
    autour = (probas >= 0.75) & (probas <= 0.85)
    print(f"\nRelevés annoncés entre 75 % et 85 % : {int(autour.sum())}")
    if autour.any():
        print(f"Sur 100 relevés comme ceux-là, sont vraiment des canulars : "
              f"{y_test[autour].mean() * 100:.0f}")

    trop = sum(l["n"] * (l["annonce"] - l["observe"]) for l in avant) / len(y_test)
    print(f"\nLe système est {'TROP CONFIANT' if trop > 0 else 'TROP PRUDENT'} : il annonce "
          f"en moyenne {abs(trop) * 100:.0f} points de\npourcentage "
          f"{'de plus' if trop > 0 else 'de moins'} que ce qui se produit.")

    # La correction s'apprend sur la validation de la répétition générale, jamais
    # sur le test. Elle est croissante, donc elle ne change pas le classement :
    # elle réécrit les nombres, pas l'ordre.
    correcteur = IsotonicRegression(out_of_bounds="clip")
    correcteur.fit(veille["probas_val"], veille["y_val"])
    corrigees = correcteur.predict(probas)

    apres = tranches_de_probabilite(y_test, corrigees)
    montrer_tranches("Après correction :", apres)

    print(f"\nAUC avant {roc_auc_score(y_test, probas):.3f}, après "
          f"{roc_auc_score(y_test, corrigees):.3f}. La correction monte par paliers :")
    print("elle met des relevés à égalité, d'où les quelques millièmes perdus, mais")
    print("elle n'en fait passer aucun devant un autre.")
    print(f"Écart moyen : {ecart_de_calibration(avant):.1%} avant, "
          f"{ecart_de_calibration(apres):.1%} après.")

    frontiere_corrigee = float(correcteur.predict([resultat13["retenue"]])[0])
    print(f"\nMa frontière de la phase 13, {resultat13['retenue']:.3f} en probabilité brute,")
    print(f"vaut {frontiere_corrigee:.1%} une fois corrigée — à comparer aux 6,25 % "
          f"du point de bascule.")
    return {"corrigees": corrigees, "correcteur": correcteur}


# Cinq endroits où poser la coupure, tous défendables.
PARTS_TEST = [0.20, 0.225, 0.25, 0.275, 0.30]
TIRAGES = 1000


def mesurer(y_test, predit, scores):
    return {
        "rappel": recall_score(y_test, predit),
        "precision": precision_score(y_test, predit, zero_division=0),
        "auc": roc_auc_score(y_test, scores),
    }


def fourchette_par_decoupe(df, df_brut, groupes, C):
    """Refait la mesure en déplaçant la coupure, et rend ce que ça donne."""
    y = df_brut["canular"].to_numpy().astype(int)
    lignes = []
    for part in PARTS_TEST:
        i_tr, i_te, coupure = decoupe_temporelle(df, groupes, part)
        chaine = Chaine(
            cols_cat=COLS_CAT_FINAL, cols_num=COLS_NUM_FINAL, C=C
        ).apprendre(df_brut[i_tr.tolist()], y[i_tr])
        predit, scores = chaine.repondre(df_brut[i_te.tolist()])
        mesure = mesurer(y[i_te], predit, scores)
        mesure.update(coupure=coupure, n_test=len(i_te), canulars=int(y[i_te].sum()))
        lignes.append(mesure)
    return lignes


def fourchette_par_tirage(y_test, probas, seuil):
    """Retire au sort, avec remise, les relevés de la partie test.

    C'est la question du Conseil prise au mot : et si trois canulars étaient
    tombés de l'autre côté ?
    """
    hasard = np.random.default_rng(GRAINE)
    predit = probas >= seuil
    rappels = []
    for _ in range(TIRAGES):
        tire = hasard.integers(0, len(y_test), len(y_test))
        vrais = y_test[tire] == 1
        if vrais.sum():
            rappels.append(predit[tire][vrais].mean())
    return np.percentile(rappels, [2.5, 97.5])


def phase15_deux_analystes(df, df_brut, groupes, C, resultat13):
    """Un chiffre nu ne veut rien dire quand il repose sur 164 relevés."""
    titre(15, "deux analystes, deux chiffres")

    y_test = resultat13["y_test"]
    print(f"Taille de la partie test          : {len(y_test)} relevés")
    print(f"Canulars qu'elle contient         : {int(y_test.sum())}")
    print(f"Un canular pèse donc              : "
          f"{100 / int(y_test.sum()):.2f} point de rappel")

    lignes = fourchette_par_decoupe(df, df_brut, groupes, C)
    print(f"\nLa même mesure, en déplaçant la coupure ({len(lignes)} découpes) :")
    print(f"{'coupure':>12}{'test':>8}{'canulars':>10}{'attrapés':>10}"
          f"{'justes':>8}{'AUC':>8}")
    for l in lignes:
        print(f"{str(l['coupure']):>12}{l['n_test']:>8}{l['canulars']:>10}"
              f"{l['rappel'] * 100:>10.0f}{l['precision'] * 100:>8.0f}{l['auc']:>8.3f}")

    rappels = [l["rappel"] for l in lignes]
    aucs = [l["auc"] for l in lignes]
    print(f"\nSur 100 canulars, attrapés : de {min(rappels) * 100:.0f} à "
          f"{max(rappels) * 100:.0f} selon la découpe")
    print(f"AUC                        : de {min(aucs):.3f} à {max(aucs):.3f}")

    bas, haut = fourchette_par_tirage(y_test, resultat13["probas"], 0.5)
    print(f"\nEn retirant au sort les relevés du test ({TIRAGES} tirages), les 95 %")
    print(f"du milieu vont de {bas * 100:.0f} à {haut * 100:.0f} canulars attrapés sur 100.")

    # J'annonce l'intervalle qui couvre les deux sources d'incertitude, pas le
    # plus flatteur des deux.
    bas_total, haut_total = min(min(rappels), bas), max(max(rappels), haut)
    largeur = (haut_total - bas_total) * 100
    print(f"\nLe nombre que j'annonce au Conseil : de {bas_total * 100:.0f} à "
          f"{haut_total * 100:.0f} canulars attrapés sur 100.")
    print(f"\nRéponse sur les deux analystes : 0,31 et 0,34 ne se départagent pas, parce")
    print(f"que ma propre mesure s'étale sur {largeur:.0f} points d'un tirage à l'autre sans")
    print("que le modèle change — trois canulars qui basculent en déplacent déjà "
          f"{3 * 100 / int(y_test.sum()):.0f}.")
    return lignes


# Une colonne du fichier d'origine à la fois. date_posted n'est plus lue par le
# modèle depuis la phase 5 : elle sert ici de témoin, sa chute doit valoir zéro.
COLONNES_A_ABIMER = [
    "comments", "city", "state", "country", "shape", "duration_seconds",
    "duration_hours_min", "datetime", "latitude", "longitude", "date_posted",
]


def importance_par_permutation(chaine, df_brut, y):
    """Abîme une colonne en mélangeant ses valeurs, et mesure la chute."""
    hasard = np.random.default_rng(GRAINE)
    reference = roc_auc_score(y, chaine.probabilites(df_brut))
    chutes = []
    for colonne in COLONNES_A_ABIMER:
        melange = hasard.permutation(df_brut[colonne].to_numpy())
        abime = df_brut.with_columns(pl.Series(colonne, melange))
        chutes.append((colonne, reference - roc_auc_score(y, chaine.probabilites(abime))))
    return reference, sorted(chutes, key=lambda c: -c[1])


def montrer_dossier(intitule, chaine, ligne, rang_fichier, proba, verite, frontiere):
    print(f"\n{intitule}")
    print(f"  relevé n° {rang_fichier} du fichier — {ligne['city'][0]} "
          f"({ligne['state'][0] or '—'}), observé le {ligne['datetime'][0]}")
    brut = (ligne["comments"][0] or "").replace("&#44", ",")
    lu = re.sub(NOTE_DU_BUREAU, "", brut).strip()
    if lu != brut.strip():
        print(f"  au dossier   : « {brut[:110]} »")
        print(f"  ce qu'il lit : « {lu[:110]} »   (la note du Bureau est retirée)")
    else:
        print(f"  « {lu[:120]} »")
    print(f"  probabilité annoncée {proba:.3f} — "
          f"{'DÉNONCÉ' if proba >= frontiere else 'laissé passer'}, "
          f"{'canular' if verite else 'honnête'} en réalité")
    print("  ce qui a pesé :")
    for nom, poids in chaine.ce_qui_a_pese(ligne):
        sens = "vers canular" if poids > 0 else "vers honnête"
        print(f"    {poids:+7.3f}  {sens:<13} {nom}")


def phase16_trois_dossiers(chaine, df_brut, decoupe, resultat13):
    """Expliquer un dossier et expliquer le système sont deux questions."""
    titre(16, "trois dossiers sur le bureau")

    _, i_test = decoupe
    y_test, probas = resultat13["y_test"], resultat13["probas"]
    frontiere = resultat13["retenue"]
    test = df_brut[i_test.tolist()]

    sur = int(np.argmax(probas))
    au_dessus = np.flatnonzero(probas >= frontiere)
    juste = int(au_dessus[np.argmin(probas[au_dessus])])
    manques = np.flatnonzero((y_test == 1) & (probas < frontiere))
    rate = int(manques[np.argmax(probas[manques])])

    for intitule, i in [
        ("DOSSIER 1 — dénoncé avec la plus forte confiance", sur),
        ("DOSSIER 2 — tout juste au-dessus de ma frontière", juste),
        ("DOSSIER 3 — un canular que j'ai laissé passer", rate),
    ]:
        montrer_dossier(intitule, chaine, test[i], int(i_test[i]) + 1,
                        probas[i], y_test[i], frontiere)

    reference, chutes = importance_par_permutation(chaine, test, y_test)
    print(f"\nSur quoi le système s'appuie globalement. J'abîme une colonne à la fois")
    print(f"en mélangeant ses valeurs au hasard, et je regarde ce que perd l'AUC")
    print(f"(référence : {reference:.3f}).")
    print(f"\n{'colonne':>20}{'AUC abîmée':>13}{'chute':>10}")
    for colonne, chute in chutes:
        print(f"{colonne:>20}{reference - chute:>13.3f}{chute:>10.3f}")
    return chutes


def phase9_cases_vides(df, decoupe, avant, C):
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

    apres = entrainer(df, texte="temoignage", avec_delai=False, C=C, decoupe=decoupe)
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
    C = regler_regularisation(df)
    avant = phase4_premier_verdict(df, C)
    honnete, meilleur = phase5_fuite_temporelle(df, avant, C)
    phase6_modele_du_stagiaire(honnete, meilleur)
    par_evenement, groupes = phase7_un_seul_evenement(df, honnete, C)
    dans_le_temps, (i_train, i_test, _) = phase8_ordre_du_temps(
        df, groupes, par_evenement, C
    )
    sans_trous, _ = phase9_cases_vides(df, (i_train, i_test), dans_le_temps, C)
    avec_etiquette = brut.with_columns(canular=df["canular"])
    en_chaine, _ = phase10_chaine_de_traitement(
        avec_etiquette, (i_train, i_test), sans_trous, C
    )
    phase11_duree(df)
    _, chaine = phase12_ville_et_heure(avec_etiquette, (i_train, i_test), en_chaine, C)
    facture13 = phase13_facture_du_bureau(
        chaine, avec_etiquette, (i_train, i_test), df, groupes, C
    )
    phase14_promesse_a_80(facture13)
    phase15_deux_analystes(df, avec_etiquette, groupes, C, facture13)
    phase16_trois_dossiers(chaine, avec_etiquette, (i_train, i_test), facture13)
    print("\nFin de l'analyse. Les chiffres sont à reporter dans RAPPORT.md.")


if __name__ == "__main__":
    main()
