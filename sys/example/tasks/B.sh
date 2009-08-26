#!/bin/bash

# cyclon example system, task B
# depends on task A and its own restart file

TMPDIR=${TMPDIR:-/tmp/$USER/example}
mkdir -p $TMPDIR

# check prerequistes
ONE=$TMPDIR/A.${REFERENCE_TIME}.1
TWO=$TMPDIR/B.${REFERENCE_TIME}.restart
for PRE in $ONE $TWO; do
    [[ ! -f $PRE ]] && {
        echo "ERROR, file not found: $PRE"
        exit 1
    }
done

# generate outputs
touch $TMPDIR/B.${REFERENCE_TIME}
touch $TMPDIR/B.${NEXT_REFERENCE_TIME}.restart
