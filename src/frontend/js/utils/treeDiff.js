// Comparing the two file trees of an integrity check (EditHashsumJobDialog). Plain JS
// (test/frontend/test_tree_diff.mjs). The trees are the backend's differing branches
// (utils/file_utils.py): [{title, children}] / {title, hash, isLeaf}, sorted by title.

export const NodeType = {
    LOADING: -1,
    IDENTICAL: 0,
    MODIFY: 1,
    MISSING: 2,
}

/**
 * Marks the nodes of both trees in place (`type`: 'modify', 'missing', 'insert', or
 * {type: 'hidden'} placeholders inserted so that both trees keep the same rows) and
 * returns the worst difference found: IDENTICAL, MODIFY (different hashes, a file only
 * on one side, a file against a folder) or MISSING (a file without a hash on a side).
 */
export function compareTrees(treeLeft, treeRight) {
    treeLeft = treeLeft || []
    treeRight = treeRight || []

    let diff = NodeType.IDENTICAL;

    // Both indexes advance together: a node without a partner gets a hidden
    // placeholder in the other tree at the same position
    let i = 0
    let j = 0
    while (i < treeLeft.length && j < treeRight.length) {
        if (treeLeft[i].title === treeRight[j].title) {
            if (isLeaf(treeLeft[i]) && isLeaf(treeRight[j])) {
                if (!treeLeft[i].hash || !treeRight[j].hash) {
                    treeLeft[i].type = treeRight[j].type = 'missing';
                    diff = Math.max(diff, NodeType.MISSING)
                } else if (treeLeft[i].hash !== treeRight[j].hash) {
                    treeLeft[i].type = treeRight[j].type = 'modify';
                    diff = Math.max(diff, NodeType.MODIFY)
                } else {
                    // The leafs are the same
                }
            } else if (isLeaf(treeLeft[i]) || isLeaf(treeRight[j])) {
                treeLeft[i].type = treeRight[j].type = 'modify';
                diff = Math.max(diff, NodeType.MODIFY)
            } else { // Both non leafs
                const subtreeDiff = compareTrees(treeLeft[i].children, treeRight[j].children)
                if (subtreeDiff === NodeType.MODIFY) {
                    treeLeft[i].type = treeRight[j].type = 'modify';
                } else if (subtreeDiff === NodeType.MISSING) {
                    treeLeft[i].type = treeRight[j].type = 'missing';
                }
                diff = Math.max(diff, subtreeDiff)
            }
        } else if (treeLeft[i].title < treeRight[j].title) {
            treeLeft[i].type = 'insert'
            treeRight.splice(i, 0, {type: 'hidden'})
            diff = Math.max(diff, NodeType.MODIFY)
        } else {
            treeRight[j].type = 'insert'
            treeLeft.splice(j, 0, {type: 'hidden'})
            diff = Math.max(diff, NodeType.MODIFY)
        }
        i++; j++;
    }

    while (i < treeLeft.length) {
        treeLeft[i].type = 'insert'
        treeRight.splice(i, 0, {type: 'hidden'})
        diff = Math.max(diff, NodeType.MODIFY)
        i++; j++;
    }

    while (j < treeRight.length) {
        treeRight[j].type = 'insert'
        treeLeft.splice(j, 0, {type: 'hidden'})
        diff = Math.max(diff, NodeType.MODIFY)
        i++; j++;
    }
    return diff;
}

function isLeaf(treeNode) {
    return treeNode.isLeaf
}
