# Thresherr Vision

## Purpose

Thresherr exists to help users standardize media libraries through clear, profile-driven rules and safe automatic processing.

It is designed for users who want clean, consistent and predictable media libraries without having to manually build complex processing workflows.

Thresherr focuses on enforcement, consistency, validation and safe replacement.

It is not a general-purpose media processing toolbox.

---

## Core idea

Thresherr exists to make media library standardization powerful without making users design complex processing workflows.

Existing tools often fall into one of two extremes:

- They are highly flexible, but require users to manually build and maintain complex processing flows.
- They are simple to use, but do not provide enough control for strict library standards.

Thresherr aims to sit between those two extremes.

The user defines the desired final standard for a media library through a Threshing Profile. Thresherr is responsible for analyzing each input file, generating the required internal processing plan and executing that plan automatically when it is valid.

The user defines the target.  
Thresherr determines the path.

---

## Profiles define the target standard

A Threshing Profile defines the desired final standard for a media library.

The profile is not a manual workflow and it is not a loose suggestion. It is the user-facing definition of what a compliant output file should look like.

However, not every profile rule has the same meaning.

Some rules are mandatory requirements. For example, the output file must comply with the configured video container, maximum video resolution, audio codec, audio channel layout and subtitle codec rules.

Some rules define maximum allowed values rather than exact targets. For example, maximum video resolution, maximum video bitrate and maximum audio bitrate define upper limits. Output files may use lower values, but must not exceed the configured maximum.

Some rules define allowed values rather than required values. For example, allowed audio and subtitle languages define which languages may be present, but they do not require every allowed language to exist in every file.

Default language rules are conditional. If the configured default audio or subtitle language exists in the file, Thresherr should use it as the default. If it does not exist, Thresherr should fall back to the original language when that information is available.

Profile rules may also influence internal processing decisions. For example, a maximum video resolution rule may affect which internal video features or encoding strategies are valid for the output file.

These internal decisions should be derived by Thresherr from the profile, not manually configured as workflow steps by the user.

This allows profiles to be strict enough to enforce consistency, while still accepting the real-world limitations of media files.

---

## Automatic execution when a valid plan exists

Thresherr should execute processing automatically when it can generate a valid processing plan for an input file based on the selected Threshing Profile.

A valid processing plan is an internal plan that Thresherr considers sufficient to transform the input file into an output file that complies with the selected profile.

Automation in Thresherr is allowed only when the system can make a clear, valid and profile-compliant decision.

When such a plan exists, the user should not be required to review, approve or manually adjust the internal processing steps for each file.

Processing details should remain hidden by default during normal operation.

The user should only be involved when Thresherr cannot generate a valid processing plan.

---

## Automatic execution, auditable afterwards

Thresherr should not require users to review or approve internal processing steps when a valid processing plan exists.

However, automatic execution must not make the system opaque.

Every completed job, whether successful or failed, should leave a clear record of what happened to the input file.

Users should be able to review past jobs and understand:

- Which input file was processed
- Which profile was applied
- Whether the job succeeded or failed
- What Thresherr attempted to do
- What changes were made to produce the output file
- Why the job failed, if it failed

This audit view is not intended to be a workflow editor.

It exists to provide transparency, troubleshooting and confidence after processing has happened.

---

## Safe replacement after validation

Thresherr may execute valid processing plans automatically, but automatic execution must not mean unsafe replacement of original files.

When processing a file, Thresherr should create a new temporary output file in the user-configured TEMP path.

The original input file should remain unchanged while processing is in progress.

After the temporary output file is created, Thresherr must validate it against the selected Threshing Profile.

Only if the temporary output file complies with the profile should Thresherr replace the original file.

If the temporary output file does not comply with the profile, Thresherr must not replace the original file.

This ensures that automatic processing remains safe, predictable and profile-compliant.

---

## Simple by default, advanced on demand

Thresherr should be approachable for non-technical users.

The primary experience should be simple, guided and safe. Users should be able to define a library profile, let Thresherr process files automatically when a valid plan exists, and review results without needing to understand codecs, streams, containers, command-line tools or processing pipelines in depth.

However, simple does not mean opaque.

Advanced technical information should be available on demand for users who want to inspect what Thresherr did, understand why a job failed or verify how a decision was made.

Advanced views are intended for visibility, diagnostics and trust.

They are not intended to let users manually edit generated processing plans, modify internal execution steps or build custom workflows.

Thresherr should feel safe and simple for beginners, while still giving advanced users enough insight to understand and trust the system.

---

## Non-goals

Thresherr is intentionally focused.

It is not meant to become a general-purpose media application or a manual workflow automation platform.

Thresherr is not:

- A media player
- A download manager
- A manual workflow builder
- A plugin-chain editor
- A general-purpose transcoding tool without library profiles
- A tool that requires users to design processing pipelines
- A tool where advanced mode is used to modify internal processing plans

Thresherr should avoid features that weaken its core purpose: helping users standardize media libraries through clear profiles, automatic processing, validation
