// language: JavaScript, file: lib/names.js
// Ported from roblox_creator/identity.py. Roblox usernames: 3-20 chars,
// [A-Za-z0-9_], no leading/trailing underscore, no doubled underscores.

const ADJECTIVES = [
  "Nova", "Lunar", "Crimson", "Frost", "Quiet", "Velvet", "Iron", "Amber",
  "Cobalt", "Dusk", "Ember", "Glacier", "Hollow", "Ivory", "Jade", "Krypton",
  "Lucid", "Moss", "Nimbus", "Onyx", "Prism", "Quartz", "Raven", "Sable",
  "Tidal", "Umber", "Vesper", "Wraith", "Xenon", "Yarrow", "Zephyr", "Basalt",
  "Cinder", "Drift", "Echo", "Fable", "Gale", "Haze", "Indigo", "Juniper",
];

const NOUNS = [
  "Fox", "Harbor", "Kite", "Lantern", "Marrow", "Nettle", "Orbit", "Pilot",
  "Quill", "Ridge", "Signal", "Thorn", "Anchor", "Beacon", "Cipher", "Delta",
  "Emberling", "Falcon", "Gravel", "Hollow", "Isle", "Junction", "Kestrel",
  "Ledge", "Meridian", "Nocturne", "Obelisk", "Pylon", "Quarry", "Rook",
  "Spire", "Tundra", "Vault", "Warden", "Xylo", "Yonder", "Zenith", "Basin",
  "Canyon", "Dune",
];

export const FIRST_NAMES = [
  "Camille", "Rafael", "Marta", "Viktor", "Amara", "Diego", "Ines", "Tomas",
  "Yuki", "Noor", "Elias", "Priya", "Mateo", "Sofia", "Kwame", "Leila",
  "Hugo", "Anika", "Omar", "Clara", "Nikolai", "Aisha", "Bruno", "Freya",
  "Ivan", "Zara", "Luca", "Maya", "Idris", "Elena", "Kai", "Rosa",
  "Anders", "Nadia", "Ravi", "Iris", "Thiago", "Mila", "Jonas", "Ayla",
];

export const LAST_NAMES = [
  "Bianchi", "Costa", "Andersen", "Nakamura", "Okafor", "Silva", "Moreau",
  "Petrov", "Tanaka", "Haddad", "Lindqvist", "Sharma", "Alvarez", "Rossi",
  "Mensah", "Bauer", "Kowalski", "Ferreira", "Ibrahim", "Novak", "Sato",
  "Dubois", "Kaur", "Larsen", "Romero", "Vogel", "Osei", "Yildiz",
  "Marchetti", "Nilsen", "Park", "Cardoso", "Horvat", "Diallo", "Sato",
  "Vargas", "Bakker", "Mendes", "Fischer", "Rahman",
];

const LOWER = "abcdefghijkmnopqrstuvwxyz";
const UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ";
const DIGIT = "23456789";
const SYMBOL = "!@#$%^&*-_";

const pick = (list) => list[Math.floor(Math.random() * list.length)];

function randomInt(min, max) {
  return min + Math.floor(Math.random() * (max - min + 1));
}

export function makeUsername(attempt = 0) {
  const style = Math.random();
  const adjective = pick(ADJECTIVES);
  const noun = pick(NOUNS);
  let name;

  if (style < 0.45) {
    name = `${adjective}${noun}${randomInt(10, 9999)}`;
  } else if (style < 0.75) {
    name = `${adjective}_${noun}${randomInt(1, 999)}`;
  } else if (style < 0.92) {
    name = `${noun}${randomInt(100, 99999)}${pick(["A", "B", "C", "X", "Z"])}`;
  } else {
    name = `${adjective.toLowerCase()}x${noun.toLowerCase()}${randomInt(1, 999)}`;
  }

  name = name.slice(0, 20).replace(/_+$/, "");
  if (name.length < 3) name = `${name}Alt${randomInt(10, 99)}`.slice(0, 20);
  if (attempt) name = (name.slice(0, 15) + randomInt(1000, 99999)).slice(0, 20).replace(/_+$/, "");
  return name;
}

export function makePassword(length) {
  const size = length || randomInt(13, 16);
  const chars = [pick(UPPER), pick(LOWER), pick(DIGIT), pick(SYMBOL)];
  const pool = LOWER + UPPER + DIGIT;
  while (chars.length < size) chars.push(pick(pool));
  for (let i = chars.length - 1; i > 0; i -= 1) {
    const j = Math.floor(Math.random() * (i + 1));
    [chars[i], chars[j]] = [chars[j], chars[i]];
  }
  return chars.join("");
}

export function makeIdentityFields(domain, index) {
  const first = pick(FIRST_NAMES);
  const last = pick(LAST_NAMES);
  const email = `${first}.${last}${index}@${domain}`.toLowerCase();
  return { first, last, email, password: makePassword() };
}
