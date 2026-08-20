import type { DataNode } from 'antd/es/tree';
import { formatBytes } from './format';
import type { ResourceFileItem } from '../types';

/** Build an antd Tree data structure from flat `/`-separated relative paths.
 * Intermediate segments become directory nodes, file leaves carry their
 * formatted size. Directories sort before files, both alphabetical. */
export function buildFileTree(
  files: ResourceFileItem[],
  formatSize: (n: number) => string = formatBytes,
): DataNode[] {
  interface DirNode {
    node: DataNode & { children: DataNode[] };
    dirs: Map<string, DirNode>;
  }
  const root: DirNode = {
    node: { key: '', title: '', children: [] },
    dirs: new Map(),
  };

  for (const file of files) {
    const segments = file.name.split('/').filter((s) => s.length > 0);
    if (segments.length === 0) continue;
    let dir = root;
    let prefix = '';
    for (const segment of segments.slice(0, -1)) {
      prefix = prefix ? `${prefix}/${segment}` : segment;
      let next = dir.dirs.get(segment);
      if (!next) {
        next = {
          node: { key: `d:${prefix}`, title: segment, children: [] },
          dirs: new Map(),
        };
        dir.dirs.set(segment, next);
        dir.node.children.push(next.node);
      }
      dir = next;
    }
    const fileName = segments[segments.length - 1];
    dir.node.children.push({
      key: `f:${prefix ? `${prefix}/` : ''}${fileName}`,
      title: `${fileName} (${formatSize(file.size)})`,
      isLeaf: true,
    });
  }

  const sortLevel = (dir: DirNode) => {
    dir.node.children.sort((a, b) => {
      const aDir = String(a.key).startsWith('d:');
      const bDir = String(b.key).startsWith('d:');
      if (aDir !== bDir) return aDir ? -1 : 1;
      return String(a.title).localeCompare(String(b.title));
    });
    dir.dirs.forEach(sortLevel);
  };
  sortLevel(root);
  return root.node.children;
}
