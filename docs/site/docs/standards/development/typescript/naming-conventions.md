# TypeScript Naming Conventions

## Purpose

Provide naming rules that optimize for clarity, consistency, and accessibility.

## TypeScript Conventions Baseline

Follow idiomatic TypeScript/Node casing as the default, with the house rules
below:

- Types (classes, interfaces, type aliases, enums): `PascalCase`
- Enum members: `PascalCase`
- Functions and methods: `camelCase`
- Local variables and parameters: `camelCase`
- Class fields: `camelCase`; use the `#private` prefix (or `private`) for
  non-public members rather than a naming hack
- Module-level constants: `UPPER_SNAKE_CASE` for true compile-time constants
  (`MAX_RETRIES`); `camelCase` for everything else, including `const` bindings
  that merely happen not to be reassigned
- Type parameters: single uppercase letter (`T`, `U`) or short `PascalCase` when
  more descriptive (`Element`, `Key`)
- Acronyms in `PascalCase`/`camelCase` contexts: capitalize only the first
  letter (`HttpClient`, not `HTTPClient`; `jsonParser`, not `jSONParser`)
- Do **not** prefix interfaces with `I` (`Instrument`, not `IInstrument`) — the
  structural type system makes the Hungarian prefix noise
- File names: `kebab-case.ts` (`exercise-state.ts`), matching the ESM import
  paths that reference them

Prefer the platform's own spelling for concepts it already names; do not invent a
competing house term for something the standard library or DOM already calls by a
clear name.

## Casing Convention Note

The variable naming rules below are adapted from Damian Conway's "Perl Best
Practices" (2005). Conway's original rules assume `snake_case` for all
identifiers. TypeScript uses `camelCase` for variables and functions and
`PascalCase` for types; where Conway specifies casing, the house casing above
takes precedence. Conway's underlying principles (descriptive names, minimum
length, complete words, grammatical consistency) are fully adopted.

## Variable Naming Rules

These rules are based on Damian Conway's "Perl Best Practices" (2005), adapted
for TypeScript and validated over long-term use.

### 1. Type-to-Variable Mapping

Variables representing a class or type instance use the `camelCase` version of
the type name.

```ts
// Correct
const instrument = new Instrument();
const exerciseState = new ExerciseState();
const practiceBlock = new PracticeBlock();

// Wrong
const inst = new Instrument();
const exState = new ExerciseState();
const block = new PracticeBlock();
```

### 2. Minimum Length: 3+ Characters

One- and two-character variable names are prohibited because they reduce
readability and accessibility.

```ts
// Correct
for (let index = 0; index < 10; index += 1) {
  const instrument = instruments[index];
  process(instrument);
}

for (const [instrumentIndex, instrument] of instruments.entries()) {
  process(instrument);
}

// Wrong
for (let i = 0; i < 10; i += 1) {
  const x = xs[i];
  process(x);
}
```

Exceptions:

- Well-established mathematical variables in limited scope (`x`, `y` for a
  five-line coordinate algorithm).
- Common domain abbreviations used across a codebase may appear as tokens:
  `id`, `db`, `api`, `env`, `app`, `url`. Use these only as clear tokens (for
  example, `instrumentId`, `dbSession`, `apiRouter`, `envName`, `appState`), not
  as single-character variables.

### 3. Complete English Words

Use complete English words, not abbreviations.

```ts
// Correct
const configuration = loadConfiguration();
const databaseSession = getSession();
let instrumentIndex = 0;

// Wrong
const cfg = loadConfiguration(); // Use configuration
const db = getSession();         // Use databaseSession
let idx = 0;                     // Use index or instrumentIndex
```

Exception: allowed domain abbreviations (for example, `id`, `db`, `api`) may
appear as tokens in identifiers. Other acronyms are acceptable only when they are
official domain terms (for example, `UTC`) and should not be shortened further.

### 4. Import Collision Handling

When two modules export the same name, disambiguate with an import alias or a
namespace import — never rely on a shadowing local.

```ts
// Correct
import { Session as DbSession } from "./db.js";
import { Session as RestSession } from "./rest.js";

const dbSession = new DbSession();
const restSession = new RestSession();

// Wrong
import { Session } from "./db.js";
// ...and a second `Session` from elsewhere — one silently wins
const session = new Session();
```

Alias prefixes are chosen contextually (for example, `Db`, `Rest`, `Net`).

### 5. Boolean Variables

Prefer `is*`, `has*`, or `can*` prefixes when they make the name read more
naturally as a true/false condition:

- `is*`: state or condition (`isValid`, `isEmpty`, `isActive`)
- `has*`: possession or presence (`hasPermission`, `hasItems`, `hasError`)
- `can*`: capability or permission (`canDelete`, `canWrite`, `canEdit`)

```ts
// Prefixes improve clarity — use them
const isValid = validate(instrument);
const hasPermission = checkAccess(user);
const canDelete = user.isAdmin || resource.owner === user;

if (isValid && hasPermission && canDelete) {
  remove(resource);
}
```

Omit the prefix when the name already reads unambiguously as a boolean without
it. Names that are verbs, verb phrases, or adjective phrases often convey boolean
intent on their own:

```ts
// Already clear without a prefix
const verifyTls = true;
const mapAttributes = true;
const strict = true;
```

Avoid bare nouns or adjectives that could be mistaken for the thing itself rather
than a condition about it:

```ts
// Ambiguous without a prefix
const valid = validate(instrument);    // Use isValid
const permission = checkAccess(user);  // Use hasPermission
const deletable = user.isAdmin;        // Use canDelete
```

### 6. Collections: Plural vs. Singular

Name collections based on how they are primarily used.

Plural for collective processing (arrays):

```ts
const instruments = query.all();
for (const instrument of instruments) {
  process(instrument);
}

const exerciseIds = exercises.map((exercise) => exercise.id);
```

Singular with `By` suffix for individual access (maps / lookup records):

```ts
const instrumentById = new Map<number, Instrument>();
for (const instrument of instruments) {
  instrumentById.set(instrument.id, instrument);
}
const instrument = instrumentById.get(42);

const exerciseByName = new Map<string, Exercise>();
const exercise = exerciseByName.get("Chromatic Scale");
```

### 7. Consistency Rules

- Syntactic consistency: if one variable uses `adjectiveNoun`, all similar
  variables use `adjectiveNoun`.
- Semantic consistency: names convey what data represents, not just its type.
- Cross-codebase consistency: the same concept uses the same name everywhere.

```ts
// Correct
function createInstrument(name: string): Instrument {
  const instrument = new Instrument(name);
  dbSession.create(instrument);
  return instrument;
}

function updateInstrument(instrument: Instrument, name: string): void {
  instrument.name = name;
  dbSession.save(instrument);
}

// Wrong
function createInstrument(name: string): Instrument {
  const inst = new Instrument(name);
  dbSession.create(inst);
  return inst;
}

function updateInstrument(instrument: Instrument, name: string): void {
  instrument.name = name;
  dbSession.save(instrument);
}
```
