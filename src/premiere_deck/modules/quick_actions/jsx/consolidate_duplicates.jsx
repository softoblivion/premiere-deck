// Runs Edit > Consolidate Duplicates (Project.consolidateDuplicates, documented).
// returns: {done}
if (!app.project) {
    deckFail("No project open.");
}
if (!app.project.consolidateDuplicates) {
    deckFail("This Premiere version cannot consolidate duplicates by script.");
}
app.project.consolidateDuplicates();
return {done: true};
