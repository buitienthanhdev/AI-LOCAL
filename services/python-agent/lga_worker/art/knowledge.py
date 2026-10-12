"""Art knowledge: a small curated base plus what this machine has learned (verified reference fetches).

Everything here is general art-history knowledge written by the developers. It is NOT a verification against an
external source: an entry resolved from this table reports `verification: "BUILTIN_KNOWLEDGE"` and the planner is told
to confirm identity with a reference picture (`art.fetch_reference`) whenever the request is a reproduction. What the
machine fetches and checks is stored separately under memory/art/ with its source URL, licence and hash.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lga_worker.art.modes import Mode, normalize


@dataclass(frozen=True)
class Artwork:
    key: str
    titles: tuple[str, ...]  # every spelling people use (any language)
    artist: str
    year: str
    movement: str
    genre: str
    medium: str  # what the original is made of
    mode: Mode  # how Paint should simulate it by default
    size: str = ""
    location: str = ""
    signature: tuple[str, ...] = ()  # what makes it recognisable
    palette: tuple[str, ...] = ()  # '#RRGGBB name'
    technique: tuple[str, ...] = ()
    commons_query: str = ""  # search words for a reference image on Wikimedia Commons
    versions: int = 1  # more than one: the title does not name one work
    note: str = ""


ARTWORKS: tuple[Artwork, ...] = (
    Artwork(
        "starry_night", ("The Starry Night", "Starry Night", "La Nuit étoilée", "De sterrennacht", "Đêm đầy sao", "Đêm sao",
                         "Starry Night Van Gogh", "Đêm đầy sao Van Gogh"),
        "Vincent van Gogh", "1889 (June)", "Post-Impressionism", "night landscape with village", "oil on canvas",
        Mode.OIL_PAINT_SIMULATION, "73.7 x 92.1 cm", "Museum of Modern Art, New York",
        ("a huge swirling sky of spiral eddies in blue and white that fills about two thirds of the canvas",
         "a large crescent moon with a glowing halo at the upper right",
         "eleven stars, each with concentric yellow-white halos",
         "a tall dark cypress flame rising from the left foreground to the top edge",
         "a small village with a church spire in the middle distance, a few windows lit yellow-orange",
         "rolling blue hills along the horizon"),
        ("#1c3a7a ultramarine", "#2f66b0 cobalt", "#8db7d9 pale blue", "#f3d34a yellow", "#f6e9a8 pale yellow",
         "#1d2433 cypress near-black", "#3f5d3a dark green", "#d98d2b window ochre"),
        ("short thick directional brushstrokes that follow flow lines", "impasto", "complementary blue / yellow contrast"),
        "The Starry Night Van Gogh 1889"),
    Artwork(
        "mona_lisa", ("Mona Lisa", "La Gioconda", "La Joconde", "Nàng Mona Lisa"), "Leonardo da Vinci", "c. 1503-1519",
        "High Renaissance", "portrait", "oil on poplar panel", Mode.OIL_PAINT_SIMULATION, "77 x 53 cm", "Louvre, Paris",
        ("half-length portrait of a seated woman, body turned three quarters, hands folded",
         "a faint smile and soft sfumato shading with no hard outlines", "a distant hazy landscape with winding paths and water behind her",
         "dark clothing and a thin veil, no eyebrows"),
        ("#3b3220 dark brown", "#6b5a2e olive brown", "#c49a63 skin ochre", "#8a8a5a hazy green", "#5e7f86 blue-grey distance"),
        ("sfumato: layered soft transitions without outlines", "glazing"), "Mona Lisa Leonardo da Vinci"),
    Artwork(
        "great_wave", ("The Great Wave off Kanagawa", "The Great Wave", "Great Wave off Kanagawa", "Under the Wave off Kanagawa",
                       "Làn sóng lớn ngoài khơi Kanagawa", "Sóng lớn Kanagawa", "神奈川沖浪裏"),
        "Katsushika Hokusai", "c. 1831", "Ukiyo-e (Edo period)", "seascape", "polychrome woodblock print, ink and colour on paper",
        Mode.FLAT_COLOR, "25.7 x 37.9 cm", "many impressions: Metropolitan Museum of Art, British Museum and others",
        ("a huge curling wave with claw-like foam fingers at the left and top", "Mount Fuji small and distant in the trough of the wave",
         "three narrow boats with rowers under the wave", "dark outlines, flat areas of Prussian blue and cream paper"),
        ("#143a64 Prussian blue", "#3d6a9a mid blue", "#a9c8dc pale blue", "#f1e7c8 cream paper", "#222a35 outline"),
        ("flat colour areas with a key block of dark outlines", "gradation of blue (bokashi)"), "Great Wave off Kanagawa Hokusai"),
    Artwork(
        "persistence_of_memory", ("The Persistence of Memory", "Persistence of Memory", "La persistencia de la memoria",
                                  "Sự dai dẳng của ký ức", "Đồng hồ chảy"),
        "Salvador Dalí", "1931", "Surrealism", "dreamscape", "oil on canvas", Mode.OIL_PAINT_SIMULATION, "24 x 33 cm",
        "Museum of Modern Art, New York",
        ("soft melting pocket watches draped over a tree branch, a table edge and a sleeping blob-like form",
         "a barren plain and a calm sea with cliffs at the horizon", "an orange pocket watch covered in ants",
         "a dark foreground ground and a luminous pale sky"),
        ("#c9a15b sand ochre", "#7b4b2a brown", "#e8c777 pale gold", "#2b3a55 dark blue", "#b9c6cf pale sky"),
        ("hyper-precise smooth realism of impossible objects",), "Persistence of Memory Dali"),
    Artwork(
        "guernica", ("Guernica",), "Pablo Picasso", "1937", "Cubism / Surrealism", "history painting (war)", "oil on canvas",
        Mode.GRAYSCALE, "349 x 776 cm", "Museo Reina Sofia, Madrid",
        ("monochrome palette of black, white and greys", "a screaming horse, a bull, a woman with a dead child, a fallen warrior",
         "a light bulb like an eye at the top centre", "fragmented angular figures on a flat shallow stage"),
        ("#111111 black", "#f2f2f2 white", "#8c8c8c grey"), ("flat angular shapes", "newsprint-like hatching"), "Guernica Picasso"),
    Artwork(
        "girl_pearl_earring", ("Girl with a Pearl Earring", "Het meisje met de parel", "Cô gái đeo hoa tai ngọc trai"),
        "Johannes Vermeer", "c. 1665", "Dutch Golden Age", "tronie (character study)", "oil on canvas", Mode.OIL_PAINT_SIMULATION,
        "44.5 x 39 cm", "Mauritshuis, The Hague",
        ("a young woman turned over her shoulder looking at the viewer, parted lips",
         "a blue and yellow turban with a trailing cloth", "a large pearl earring catching a highlight",
         "a plain dark background"),
        ("#1a1815 near-black ground", "#2b4a7a ultramarine", "#d9a93a yellow ochre", "#e0b79c skin", "#e9e7e0 pearl"),
        ("smooth glazes and soft light from the left",), "Girl with a Pearl Earring Vermeer"),
    Artwork(
        "night_watch", ("The Night Watch", "De Nachtwacht", "Night Watch Rembrandt"), "Rembrandt van Rijn", "1642", "Baroque",
        "group portrait", "oil on canvas", Mode.OIL_PAINT_SIMULATION, "363 x 437 cm", "Rijksmuseum, Amsterdam",
        ("a militia company stepping forward out of a dark archway", "a captain in black and a lieutenant in bright yellow at the centre",
         "a girl in yellow with a chicken at the left", "dramatic light and shadow"),
        ("#1b1510 dark brown", "#8a6a2f ochre", "#e3c86a yellow", "#8c2b1e red", "#d8cdb0 pale"),
        ("chiaroscuro",), "The Night Watch Rembrandt"),
    Artwork(
        "the_kiss_klimt", ("The Kiss Klimt", "Der Kuss", "@The Kiss", "@Nụ hôn", "Nụ hôn Klimt"), "Gustav Klimt", "1907-1908",
        "Vienna Secession / Symbolism", "figure painting", "oil and gold leaf on canvas", Mode.ACRYLIC_SIMULATION, "180 x 180 cm",
        "Belvedere, Vienna",
        ("an embracing couple wrapped in one gold robe on a flowery meadow edge", "ornamental patterns: rectangles on him, circles and flowers on her",
         "a gold shimmering background", "a flat decorative treatment with faces and hands modelled naturally"),
        ("#c99a2e gold", "#7a5a1e dark gold", "#e8d7a0 pale gold", "#3b2b25 dark brown", "#c4553b red accent"),
        ("gold leaf", "flat ornamental patterning"), "The Kiss Klimt"),
    Artwork(
        "the_scream", ("The Scream", "Skrik", "Der Schrei", "Tiếng thét"), "Edvard Munch", "1893-1910", "Expressionism",
        "psychological landscape", "several versions (tempera and crayon on board, pastel, lithograph)", Mode.PAINTERLY,
        versions=4, note="Munch made four main versions and a lithograph; the one meant must be known"),
    Artwork(
        "sunflowers", ("@Sunflowers", "Vase with Fifteen Sunflowers", "Hoa hướng dương Van Gogh", "@Hoa hướng dương"),
        "Vincent van Gogh", "1887-1889", "Post-Impressionism", "still life", "oil on canvas", Mode.OIL_PAINT_SIMULATION,
        versions=7, note="a series of sunflower still lifes: several vases and several canvases carry this title"),
    Artwork(
        "water_lilies", ("@Water Lilies", "Nymphéas", "Hoa súng Monet", "@Hoa súng"), "Claude Monet", "1896-1926", "Impressionism",
        "landscape", "oil on canvas", Mode.PAINTERLY, versions=250, note="about 250 canvases carry this title"),
    Artwork(
        "nighthawks", ("Nighthawks", "Những con cú đêm"), "Edward Hopper", "1942", "American Scene / Realism", "urban night scene",
        "oil on canvas", Mode.OIL_PAINT_SIMULATION, "84.1 x 152.4 cm", "Art Institute of Chicago",
        ("a corner diner lit by fluorescent light, glass wrapping the corner", "three customers and a server inside, none looking at each other",
         "an empty dark street and a dark shopfront in front", "a long diagonal sill of light and a red hat"),
        ("#1f3a2f dark green", "#c8d46a pale yellow-green", "#7a2a1c brick red", "#0d1a14 near-black", "#e9e2a8 light"),
        ("flat clean planes", "sharp artificial light"), "Nighthawks Hopper"),
    Artwork(
        "cafe_terrace", ("Café Terrace at Night", "Cafe Terrace at Night", "Terrasse du café le soir", "Quán cà phê ban đêm"),
        "Vincent van Gogh", "1888 (September)", "Post-Impressionism", "night scene", "oil on canvas", Mode.OIL_PAINT_SIMULATION,
        "80.7 x 65.3 cm", "Kröller-Müller Museum, Otterlo",
        ("a yellow-lit cafe terrace at the left with an awning and tables", "a cobbled street in warm orange and blue",
         "a deep blue night sky with large white-yellow stars", "dark figures and a tree at the right"),
        ("#e8b52c yellow", "#d98324 orange", "#1a2f6b deep blue", "#4a78b8 mid blue", "#f4efd0 star white"),
        ("complementary blue / yellow", "short broken strokes"), "Cafe Terrace at Night Van Gogh"),
    Artwork(
        "bedroom_arles", ("Bedroom in Arles", "La Chambre à Arles", "Phòng ngủ ở Arles"), "Vincent van Gogh", "1888-1889",
        "Post-Impressionism", "interior", "oil on canvas", Mode.OIL_PAINT_SIMULATION, versions=3,
        note="Van Gogh painted three versions (Van Gogh Museum, Art Institute of Chicago, Musée d'Orsay)"),
    Artwork(
        "birth_of_venus", ("The Birth of Venus", "La nascita di Venere", "Sự ra đời của thần Vệ Nữ"), "Sandro Botticelli", "c. 1484-1486",
        "Early Renaissance", "mythological scene", "tempera on canvas", Mode.FLAT_COLOR, "172.5 x 278.9 cm", "Uffizi, Florence",
        ("Venus standing nude on a giant scallop shell, long golden hair", "Zephyr and a woman blowing her in from the left",
         "a lady with a flowered cloak waiting at the right shore", "a calm sea and sky in pale blue-green"),
        ("#a8c9b5 sea green", "#e8d3b0 skin pale", "#d9b45a gold", "#cfe0dc sky", "#6a8a5a leaf green"),
        ("linear contours", "light tempera colour"), "The Birth of Venus Botticelli"),
    Artwork(
        "american_gothic", ("American Gothic", "Gothic Mỹ"), "Grant Wood", "1930", "Regionalism", "double portrait",
        "oil on beaverboard", Mode.OIL_PAINT_SIMULATION, "78 x 65.3 cm", "Art Institute of Chicago",
        ("a stern farmer holding a pitchfork beside a woman in an apron and cameo brooch", "a white farmhouse with a pointed Gothic window behind them",
         "a symmetrical vertical composition"),
        ("#e8e4d4 white house", "#2a2d2a dark clothes", "#6a7a4a green", "#c9a789 skin"),
        ("precise hard-edged realism",), "American Gothic Grant Wood"),
    Artwork(
        "liberty_leading", ("Liberty Leading the People", "La Liberté guidant le peuple", "Nữ thần Tự do dẫn dắt nhân dân"),
        "Eugène Delacroix", "1830", "Romanticism", "history painting", "oil on canvas", Mode.OIL_PAINT_SIMULATION, "260 x 325 cm",
        "Louvre, Paris",
        ("a bare-breasted woman in a Phrygian cap raising the tricolour flag over a barricade", "a boy with pistols and men with muskets beside her",
         "fallen bodies in the foreground and smoke over Paris behind"),
        ("#1d2a4a blue", "#ecece6 white", "#b22a2a red", "#6a5a44 brown smoke", "#d8b887 sky glow"),
        ("pyramidal composition", "dramatic light"), "Liberty Leading the People Delacroix"),
    Artwork(
        "wanderer_fog", ("Wanderer above the Sea of Fog", "Der Wanderer über dem Nebelmeer", "Kẻ lữ hành trên biển sương mù"),
        "Caspar David Friedrich", "c. 1818", "Romanticism", "landscape with figure", "oil on canvas", Mode.OIL_PAINT_SIMULATION,
        "94.8 x 74.8 cm", "Kunsthalle Hamburg",
        ("a man in a dark coat seen from behind standing on a rock, centred", "a sea of fog filling the valley with peaks emerging",
         "a pale clouded sky and a dark foreground rock"),
        ("#2b2a2a dark coat", "#8fa6b8 blue-grey fog", "#d8d4c8 pale sky", "#5a5a52 rock"),
        ("atmospheric depth", "vertical composition"), "Wanderer above the Sea of Fog Friedrich"),
    Artwork(
        "impression_sunrise", ("Impression, Sunrise", "Impression, soleil levant", "Ấn tượng, mặt trời mọc"), "Claude Monet", "1872",
        "Impressionism", "harbour landscape", "oil on canvas", Mode.PAINTERLY, "48 x 63 cm", "Musée Marmottan Monet, Paris",
        ("a small bright orange sun low over a grey-blue harbour", "its orange reflection as short strokes on the water",
         "dim silhouettes of boats and cranes in the haze"),
        ("#e8702a orange sun", "#7e96a8 grey blue", "#4a5f6e dark blue", "#c4cdd0 haze"),
        ("loose broken brushwork", "equal luminance of sun and sky"), "Impression Sunrise Monet"),
    Artwork(
        "las_meninas", ("Las Meninas", "Các tiểu thư cung đình"), "Diego Velázquez", "1656", "Spanish Baroque", "group portrait",
        "oil on canvas", Mode.OIL_PAINT_SIMULATION, "318 x 276 cm", "Museo del Prado, Madrid",
        ("the Infanta Margarita in a wide white dress in the centre with her maids", "the painter at a large canvas on the left",
         "a mirror on the back wall showing the king and queen", "a tall dim room with an open doorway and a man in it"),
        ("#2a2218 dark brown", "#c9b48a cream dress", "#8a2a2a red", "#e6d8b8 light"),
        ("chiaroscuro", "loose handling of light"), "Las Meninas Velazquez"),
    Artwork(
        "creation_adam", ("The Creation of Adam", "Sự sáng tạo Adam", "Creazione di Adamo"), "Michelangelo", "c. 1512",
        "High Renaissance", "religious fresco", "fresco", Mode.FLAT_COLOR, "280 x 570 cm", "Sistine Chapel, Vatican",
        ("Adam reclining at the left with a limp arm", "God borne by angels in a red cloak at the right reaching out",
         "the two index fingers almost touching at the centre"),
        ("#e0b898 skin", "#a8452e red cloak", "#8c9a8a green-grey ground", "#d8d0c0 plaster sky"),
        ("fresco modelling",), "Creation of Adam Michelangelo"),
    Artwork(
        "last_supper", ("The Last Supper", "L'Ultima Cena", "Bữa tiệc ly"), "Leonardo da Vinci", "c. 1495-1498", "High Renaissance",
        "religious mural", "tempera and oil on plaster", Mode.OIL_PAINT_SIMULATION, "460 x 880 cm",
        "Santa Maria delle Grazie, Milan",
        ("Christ in the centre at a long table, twelve apostles in four groups of three", "one-point perspective into a room with three windows",
         "symmetrical, calm centre and agitated groups"),
        ("#6a5a42 brown", "#b8a888 plaster", "#3a4a6a blue robe", "#8a3a30 red robe"),
        ("linear perspective",), "The Last Supper Leonardo da Vinci"),
    Artwork(
        "doi_chuot_dong_ho", ("Đám cưới chuột", "Dam cuoi chuot", "The Mice's Wedding", "Mouse wedding"), "Đông Hồ folk artists",
        "traditional", "Đông Hồ folk woodblock print", "folk genre", "woodblock print on điệp paper", Mode.FLAT_COLOR,
        ("a procession of mice carrying a gift to a cat (a bribe) in front of the wedding party", "black outlines and flat natural colours",
         "a satire on corruption, two mice bearers with a sedan-chair offering"),
        ("#1a1a1a black outline", "#d9c9a0 điệp paper", "#c2402a red", "#e0b43a yellow", "#4a8a5a green"),
        ("black key block, flat colour blocks printed in sequence",), "Dam cuoi chuot Dong Ho"),
    Artwork(
        "thieu_nu_hoa_hue", ("Thiếu nữ bên hoa huệ", "Girl beside the lilies", "Young woman with lilies"), "Tô Ngọc Vân", "1943",
        "Vietnamese modern painting (École des Beaux-Arts de l'Indochine)", "portrait", "oil on canvas", Mode.OIL_PAINT_SIMULATION,
        ("a young woman in a white áo dài seen in profile, hands resting beside a vase of white lilies", "soft light, calm pale background",
         "a gentle white-and-warm-grey palette"),
        ("#f1ece0 white", "#d9c8a8 warm cream", "#8a7a64 shadow", "#3a2f26 hair"),
        ("soft modelling",), "Thiếu nữ bên hoa huệ Tô Ngọc Vân"),
    Artwork(
        "son_of_man", ("The Son of Man", "Le Fils de l'homme", "Con trai của người"), "René Magritte", "1964", "Surrealism",
        "portrait", "oil on canvas", Mode.OIL_PAINT_SIMULATION, "116 x 89 cm", "private collection",
        ("a man in a bowler hat and overcoat in front of a low wall", "a green apple hanging in front of his face",
         "a grey cloudy sky and a sea behind the wall"),
        ("#2a2f38 dark coat", "#7aa04a green apple", "#a8b6c2 sky", "#6a7a82 sea"),
        ("smooth precise realism",), "The Son of Man Magritte"),

)

MOVEMENTS: dict[str, dict[str, Any]] = {
    "post_impressionism": {"label": "Post-Impressionism", "period": "c. 1886-1905", "traits": [
        "strong colour, often unmixed", "visible directional brushwork", "subject treated for emotion and structure rather than optical effect"]},
    "impressionism": {"label": "Impressionism", "period": "c. 1860s-1880s", "traits": [
        "broken colour and short touches", "light and atmosphere over line", "everyday outdoor subjects"]},
    "expressionism": {"label": "Expressionism", "period": "c. 1905-1930s", "traits": [
        "distorted forms and heightened colour for feeling", "bold outlines", "emotional intensity"]},
    "surrealism": {"label": "Surrealism", "period": "c. 1920s-1950s", "traits": [
        "dream imagery rendered with precise realism", "impossible juxtapositions", "empty deep space"]},
    "cubism": {"label": "Cubism", "period": "c. 1907-1920s", "traits": [
        "fragmented forms seen from several viewpoints", "angular planes", "restricted palette"]},
    "ukiyo_e": {"label": "Ukiyo-e", "period": "Edo period, 17th-19th c.", "traits": [
        "woodblock print: key-block outlines with flat colour areas", "gradation printed in blocks", "strong compositions"]},
    "renaissance": {"label": "High Renaissance", "period": "c. 1490-1527", "traits": [
        "idealised proportion and balance", "soft modelling of light (sfumato, chiaroscuro)", "perspective"]},
    "baroque": {"label": "Baroque", "period": "c. 1600-1750", "traits": ["dramatic light and shadow", "movement and diagonal composition"]},
    "vietnamese_lacquer": {"label": "Vietnamese lacquer painting (tranh sơn mài)", "period": "modern, from the 1930s", "traits": [
        "layers of natural resin lacquer polished flat", "gold and silver leaf and crushed eggshell inlaid in the surface",
        "deep red, black and gold palette with a glossy depth", "flat decorative composition"]},
    "dong_ho": {"label": "Đông Hồ folk woodblock prints", "period": "traditional", "traits": [
        "woodblock outlines in black", "flat natural colours printed in separate blocks on điệp paper",
        "simple symbolic motifs"]},
}

MEDIA: dict[str, dict[str, Any]] = {
    "oil": {"mode": Mode.OIL_PAINT_SIMULATION, "traits": ["covering opaque paint", "visible directional strokes", "layers and gradual blending"],
            "paint": "Oil brush strokes laid coarse to fine along the shapes of the image"},
    "acrylic": {"mode": Mode.ACRYLIC_SIMULATION, "traits": ["opaque, quick-drying", "flat blocks or visible strokes"],
                "paint": "flat colour blocks with a layer of broad strokes on top"},
    "watercolor": {"mode": Mode.WATERCOLOR_SIMULATION, "traits": ["transparent washes", "paper left white for highlights", "soft wet edges"],
                   "paint": "pale tints of the colours, no ink outlines, white where the picture is lightest, soft darker edges"},
    "lacquer": {"mode": Mode.LACQUER_ART_SIMULATION, "traits": ["deep glossy dark ground", "gold and eggshell inlay", "red, black and gold"],
                "paint": "dark ground, red and gold regions, gold edge lines and eggshell-white flecks"},
    "pencil": {"mode": Mode.PENCIL_SKETCH, "traits": ["thin grey lines", "tone built from hatching", "paper stays white"],
               "paint": "thin grey contours and hatching, no colour"},
    "charcoal": {"mode": Mode.CHARCOAL_SKETCH, "traits": ["broad soft dark strokes", "strong tonal masses"],
                 "paint": "broad dark crayon strokes in neutral greys"},
    "ink": {"mode": Mode.INK_LINE_ART, "traits": ["solid black lines", "no tone except hatching and solid blacks"],
            "paint": "black contours and solid black shapes only"},
    "pixel": {"mode": Mode.PIXEL_ART, "traits": ["coarse grid", "tiny palette"], "paint": "a grid of squares in a small palette"},
}

# Artists whose name in a request means "a real artwork may be meant"
KNOWN_ARTISTS = ("van gogh", "monet", "picasso", "da vinci", "leonardo", "dali", "hokusai", "klimt", "munch", "vermeer", "rembrandt",
                 "matisse", "cezanne", "renoir", "degas", "manet", "goya", "botticelli", "michelangelo", "raphael", "kandinsky",
                 "mondrian", "warhol", "frida kahlo", "nguyen gia tri", "bui xuan phai", "to ngoc van", "nguyen phan chanh",
                 "pham van don", "luong xuan nhi", "nguyen tu nghiem", "dong ho")


@dataclass
class ReferenceRecord:
    artwork_key: str
    path: str
    source_url: str
    page_url: str
    title: str
    license: str
    sha256: str
    retrieved_at: str
    width: int = 0
    height: int = 0
    note: str = ""


@dataclass
class ArtKnowledgeStore:
    """Built-in table + a JSON file of references this machine has fetched."""

    root: Path | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _file(self) -> Path | None:
        return None if self.root is None else self.root / "art" / "references.json"

    def artworks(self) -> tuple[Artwork, ...]:
        return ARTWORKS

    def by_key(self, key: str) -> Artwork | None:
        return next((a for a in ARTWORKS if a.key == key), None)

    def find(self, text: str) -> list[tuple[Artwork, float]]:
        """Artworks whose title (or title + artist) is in `text`, best first."""
        t = f" {normalize(text)} "
        found: list[tuple[Artwork, float]] = []
        for work in ARTWORKS:
            best = 0.0
            surname = normalize(work.artist).split()[-1]
            artist_in = f" {surname} " in t
            for title in work.titles:
                needs_artist = title.startswith("@")
                n = normalize(title[1:] if needs_artist else title)
                if len(n) >= 5 and f" {n} " in t and (artist_in or not needs_artist):
                    best = max(best, 0.7 + min(0.25, len(n) / 100))
            if best and artist_in:
                best = min(1.0, best + 0.15)
            if best:
                found.append((work, round(best, 2)))
        return sorted(found, key=lambda x: -x[1])

    def artist_mentioned(self, text: str) -> str | None:
        t = f" {normalize(text)} "
        return next((a for a in KNOWN_ARTISTS if f" {a} " in t), None)

    # ---- fetched references
    def references(self) -> list[ReferenceRecord]:
        path = self._file()
        if path is None or not path.is_file():
            return []
        try:
            return [ReferenceRecord(**r) for r in json.loads(path.read_text("utf-8"))]
        except (OSError, ValueError, TypeError):
            return []

    def reference_for(self, key: str) -> ReferenceRecord | None:
        for r in reversed(self.references()):
            if r.artwork_key == key and Path(r.path).is_file():
                return r
        return None

    def remember_reference(self, record: ReferenceRecord) -> None:
        path = self._file()
        if path is None:
            return
        with self._lock:
            records = [r for r in self.references() if not (r.artwork_key == record.artwork_key and r.sha256 == record.sha256)]
            records.append(record)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps([r.__dict__ for r in records], ensure_ascii=False, indent=1), encoding="utf-8")
