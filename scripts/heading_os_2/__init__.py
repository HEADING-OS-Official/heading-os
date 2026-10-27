"""HEADING OS 2: the family/leaf vocabulary, the manifest, and the seam.

`families` is the vocabulary: the families, their leaves, the sensitivity
ladder, and the ordered path rules that map the data tree onto them. It is
data, kept in the engine so every data tree reads one definition.

`kinds` names the record kinds and where their homes sit; `states` holds the
four manifest-row states; `manifest` applies the vocabulary to a real tree and
writes the two tables.

`resolve` is the seam. A caller names a family and a leaf, and one resolver
answers with the directory where those records physically live, so a call site
never carries a physical path and a move edits the manifest, not the caller.
"""
