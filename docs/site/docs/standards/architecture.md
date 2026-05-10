# Architecture Standards

## Purpose

Define architectural standards and constraints that govern design and
implementation decisions. These standards are normative, not descriptive.

## Canonical vs. derivative artifacts

Canonical artifacts are sources of truth and must be open, inspectable, and
tool-independent.

Derivative artifacts are projections or renderings and may be lossy.

## Proprietary tools and formats

Proprietary tools and formats are permitted but constrained.

They are optional integrations and must never be authoritative. A repository
must not depend on proprietary formats for defining or interpreting canonical
data.

## File format standards

Prefer text-based, declarative, diff-friendly formats for canonical data.

Binary-only formats are discouraged for sources of truth.

## Tool and domain independence

Core artifacts must remain tool-agnostic. Tools may assist, but they must not
define meaning or introduce hidden dependencies.

## Versioning and stability

All documents are explicitly versioned.

Breaking changes require version increments and explicit rationale.

## Scope notes

UI design, rendering aesthetics, and performance optimizations are out of scope
for these standards.
