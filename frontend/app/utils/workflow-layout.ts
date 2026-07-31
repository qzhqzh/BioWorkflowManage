import type { Edge, GraphNode, XYPosition } from '@vue-flow/core'
import type { ELK, ElkNode, ElkPort } from 'elkjs/lib/elk-api'

export type WorkflowLayoutDirection = 'RIGHT' | 'DOWN'

interface WorkflowPort {
  name: string
}

interface WorkflowLayoutNodeData {
  kind: 'input' | 'tool' | 'output'
  inputs?: WorkflowPort[]
  outputs?: WorkflowPort[]
}

const DEFAULT_NODE_WIDTH = 216
const DEFAULT_NODE_HEIGHT = 112
const DEFAULT_TOOL_WIDTH = 288
const DEFAULT_TOOL_HEIGHT = 160

let elkInstancePromise: Promise<ELK> | undefined

async function getElkInstance() {
  if (!elkInstancePromise) {
    elkInstancePromise = Promise.all([
      import('elkjs/lib/elk-api.js'),
      import('elkjs/lib/elk-worker.min.js?url'),
    ]).then(([{ default: ELKConstructor }, { default: workerUrl }]) =>
      new ELKConstructor({
        algorithms: ['layered'],
        workerUrl,
      }),
    )
  }

  return elkInstancePromise
}

function getNodeDimensions(node: GraphNode) {
  const data = node.data as WorkflowLayoutNodeData
  const fallbackWidth = data.kind === 'tool' ? DEFAULT_TOOL_WIDTH : DEFAULT_NODE_WIDTH
  const fallbackHeight = data.kind === 'tool' ? DEFAULT_TOOL_HEIGHT : DEFAULT_NODE_HEIGHT

  return {
    width: node.dimensions.width || (typeof node.width === 'number' ? node.width : fallbackWidth),
    height: node.dimensions.height || (typeof node.height === 'number' ? node.height : fallbackHeight),
  }
}

function createPort(
  nodeId: string,
  handleId: string,
  side: 'NORTH' | 'EAST' | 'SOUTH' | 'WEST',
  index: number,
): ElkPort {
  return {
    id: `${nodeId}::${handleId}`,
    width: 10,
    height: 10,
    layoutOptions: {
      'org.eclipse.elk.port.side': side,
      'org.eclipse.elk.port.index': String(index),
    },
  }
}

function getVisualPortIndex(
  side: 'NORTH' | 'EAST' | 'SOUTH' | 'WEST',
  index: number,
  total: number,
) {
  return side === 'WEST' || side === 'SOUTH'
    ? total - index - 1
    : index
}

function getPorts(node: GraphNode, direction: WorkflowLayoutDirection) {
  const data = node.data as WorkflowLayoutNodeData
  const inputSide = direction === 'RIGHT' ? 'WEST' : 'NORTH'
  const outputSide = direction === 'RIGHT' ? 'EAST' : 'SOUTH'

  if (data.kind === 'input') {
    return [createPort(node.id, 'out:value', outputSide, 0)]
  }

  if (data.kind === 'output') {
    return [createPort(node.id, 'in:value', inputSide, 0)]
  }

  const inputPorts = (data.inputs ?? []).map((port, index) =>
    createPort(
      node.id,
      `in:${port.name}`,
      inputSide,
      getVisualPortIndex(inputSide, index, data.inputs?.length ?? 0),
    ),
  )
  const outputPorts = (data.outputs ?? []).map((port, index) =>
    createPort(
      node.id,
      `out:${port.name}`,
      outputSide,
      getVisualPortIndex(outputSide, index, data.outputs?.length ?? 0),
    ),
  )

  return [...inputPorts, ...outputPorts]
}

function edgeEndpoint(nodeId: string, handleId: string | null | undefined) {
  return handleId ? `${nodeId}::${handleId}` : nodeId
}

export async function layoutWorkflow(
  nodes: GraphNode[],
  edges: Edge[],
  direction: WorkflowLayoutDirection,
): Promise<Map<string, XYPosition>> {
  const graph: ElkNode = {
    id: 'workflow-root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': direction,
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.padding': '[top=24,left=24,bottom=24,right=24]',
      'elk.spacing.nodeNode': direction === 'RIGHT' ? '40' : '48',
      'elk.layered.spacing.nodeNodeBetweenLayers': direction === 'RIGHT' ? '96' : '88',
      'elk.layered.considerModelOrder.strategy': 'NODES_AND_EDGES',
      'elk.layered.considerModelOrder.portModelOrder': 'true',
      'elk.layered.nodePlacement.strategy': 'BRANDES_KOEPF',
    },
    children: nodes.map((node) => ({
      id: node.id,
      ...getNodeDimensions(node),
      ports: getPorts(node, direction),
      layoutOptions: {
        'org.eclipse.elk.portConstraints': 'FIXED_ORDER',
      },
    })),
    edges: edges.map((edge) => ({
      id: edge.id,
      sources: [edgeEndpoint(edge.source, edge.sourceHandle)],
      targets: [edgeEndpoint(edge.target, edge.targetHandle)],
    })),
  }

  const elk = await getElkInstance()
  const result = await elk.layout(graph)
  const positions = new Map<string, XYPosition>()

  for (const node of result.children ?? []) {
    positions.set(node.id, {
      x: node.x ?? 0,
      y: node.y ?? 0,
    })
  }

  return positions
}
