# C++ Naming Conventions

## Purpose

Provide naming rules that optimize for clarity, consistency, and accessibility.

## C++ Conventions Baseline

Follow the [C++ Core Guidelines](https://isocpp.github.io/CppCoreGuidelines/)
naming and layout guidance as the default, with the house casing below:

- Types (classes, structs, enums, type aliases, concepts): `PascalCase`
- Functions and methods: `snake_case`
- Local variables and parameters: `snake_case`
- Data members: `snake_case`, with a trailing underscore for private members
  (`buffer_`) to distinguish them from locals and parameters
- Constants and `constexpr` values: `PascalCase` (`MaxRetries`) or the enum
  scope they live in; avoid `UPPER_SNAKE_CASE` for these
- Macros: `UPPER_SNAKE_CASE` — and reserved for cases a `constexpr`, `inline`,
  or template cannot express, since macros ignore scope
- Namespaces: `snake_case`, short, no underscores where a single word suffices
- Template parameters: single uppercase letter (`T`, `U`) or short `PascalCase`
  when more descriptive (`Element`, `Alloc`)
- Enumerators in a scoped `enum class`: `PascalCase`
- Acronyms in `PascalCase` contexts: capitalize only the first letter
  (`HttpClient`, not `HTTPClient`; `JsonParser`, not `JSONParser`)

Prefer the standard library's spelling for concepts it already names; do not
invent a competing house term for something `std` already calls by a clear name.

## Casing Convention Note

The variable naming rules below are adapted from Damian Conway's "Perl Best
Practices" (2005). Conway's original rules assume `snake_case` for all
identifiers. C++ uses `snake_case` for variables and functions and `PascalCase`
for types; where Conway specifies casing, the house casing above takes
precedence. Conway's underlying principles (descriptive names, minimum length,
complete words, grammatical consistency) are fully adopted.

## Variable Naming Rules

These rules are based on Damian Conway's "Perl Best Practices" (2005), adapted
for C++ and validated over long-term use.

### 1. Type-to-Variable Mapping

Variables representing a class instance use the `snake_case` version of the type
name.

```cpp
// Correct
auto instrument = Instrument{};
auto exercise_state = ExerciseState{};
auto practice_block = PracticeBlock{};

// Wrong
auto inst = Instrument{};
auto ex_state = ExerciseState{};
auto block = PracticeBlock{};
```

### 2. Minimum Length: 3+ Characters

One- and two-character variable names are prohibited because they reduce
readability and accessibility.

```cpp
// Correct
for (int index = 0; index < 10; ++index) {
    const auto& instrument = instruments[index];
    process(instrument);
}

const auto count = instruments.size();
for (std::size_t instrument_index = 0; instrument_index < count; ++instrument_index) {
    process(instruments[instrument_index]);
}

// Wrong
for (int i = 0; i < 10; ++i) {
    const auto& x = xs[i];
    process(x);
}
```

Exceptions:

- C++-idiomatic short variables in tight scope (five lines or fewer): `i` in a
  single-level index loop, `it` for an iterator in a short range, `os`/`is` for
  a stream parameter in a one-line operator. These are entrenched idioms every
  C++ developer reads fluently. Outside tight scope, use descriptive names
  (`index`, `iterator`, `connection_error`).
- Well-established mathematical variables in limited scope (`x`, `y` for a
  five-line coordinate algorithm).
- Common domain abbreviations used across a codebase may appear as tokens:
  `id`, `db`, `api`, `env`, `app`. Use these only as clear tokens (for example,
  `instrument_id`, `db_session`, `api_router`, `env_name`, `app_state`), not as
  single-character variables.

### 3. Complete English Words

Use complete English words, not abbreviations.

```cpp
// Correct
auto configuration = load_configuration();
auto database_session = get_session();
int instrument_index = 0;

// Wrong
auto cfg = load_configuration();    // Use configuration
auto db = get_session();            // Use database_session
int idx = 0;                        // Use index or instrument_index
```

Exception: allowed domain abbreviations (for example, `id`, `db`, `api`) may
appear as tokens in identifiers. Other acronyms are acceptable only when they
are official domain terms (for example, `UTC`) and should not be shortened
further.

### 4. Namespace Collision Handling

When two namespaces export the same name, disambiguate with a namespace alias or
a qualified name — never a blanket `using namespace` in a header.

```cpp
// Correct
namespace fs = std::filesystem;
namespace pt = boost::property_tree;

auto path = fs::path{"/data"};
auto tree = pt::ptree{};

// Wrong
using namespace std::filesystem;
using namespace boost::property_tree;

auto path = path{"/data"};   // Which path?
```

Alias prefixes are chosen contextually (for example, `fs`, `pt`, `net`).

### 5. Boolean Variables

Prefer `is_*`, `has_*`, or `can_*` prefixes when they make the name read more
naturally as a true/false condition:

- `is_*`: state or condition (`is_valid`, `is_empty`, `is_active`)
- `has_*`: possession or presence (`has_permission`, `has_items`, `has_error`)
- `can_*`: capability or permission (`can_delete`, `can_write`, `can_edit`)

```cpp
// Prefixes improve clarity — use them
const bool is_valid = validate(instrument);
const bool has_permission = check_access(user);
const bool can_delete = user.is_admin() || resource.owner == user;

if (is_valid && has_permission && can_delete) {
    remove(resource);
}
```

Omit the prefix when the name already reads unambiguously as a boolean without
it. Names that are verbs, verb phrases, or adjective phrases often convey boolean
intent on their own:

```cpp
// Already clear without a prefix
const bool verify_tls = true;
const bool map_attributes = true;
const bool strict = true;
```

Avoid bare nouns or adjectives that could be mistaken for the thing itself rather
than a condition about it:

```cpp
// Ambiguous without a prefix
const bool valid = validate(instrument);       // Use is_valid
const bool permission = check_access(user);    // Use has_permission
const bool deletable = user.is_admin();        // Use can_delete
```

### 6. Collections: Plural vs. Singular

Name collections based on how they are primarily used.

Plural for collective processing (vectors, spans):

```cpp
auto instruments = query.all();
for (const auto& instrument : instruments) {
    process(instrument);
}

std::vector<std::int64_t> exercise_ids;
exercise_ids.reserve(exercises.size());
for (const auto& exercise : exercises) {
    exercise_ids.push_back(exercise.id);
}
```

Singular with `by_` suffix for individual access (maps):

```cpp
std::unordered_map<std::int64_t, Instrument> instrument_by_id;
for (const auto& instrument : instruments) {
    instrument_by_id.emplace(instrument.id, instrument);
}
const auto& instrument = instrument_by_id.at(42);

std::unordered_map<std::string, Exercise> exercise_by_name;
const auto& exercise = exercise_by_name.at("Chromatic Scale");
```

### 7. Consistency Rules

- Syntactic consistency: if one variable uses `adjective_noun`, all similar
  variables use `adjective_noun`.
- Semantic consistency: names convey what data represents, not just its type.
- Cross-codebase consistency: the same concept uses the same name everywhere.

```cpp
// Correct
Instrument create_instrument(const std::string& name) {
    auto instrument = Instrument{name};
    db_session.create(instrument);
    return instrument;
}

void update_instrument(Instrument& instrument, const std::string& name) {
    instrument.name = name;
    db_session.save(instrument);
}

// Wrong
Instrument create_instrument(const std::string& name) {
    auto inst = Instrument{name};
    db_session.create(inst);
    return inst;
}

void update_instrument(Instrument& instrument, const std::string& name) {
    instrument.name = name;
    db_session.save(instrument);
}
```
