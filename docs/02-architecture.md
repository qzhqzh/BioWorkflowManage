# BioWorkflowManage 技术架构设计

## 总体架构

Frontend

- Nuxt 4
- Vue 3
- TypeScript
- Vue Flow

Backend

- Django 5.2 LTS
- Django REST Framework
- PostgreSQL

Compiler Core

- Python Package
- Pydantic Schema
- Jinja2 Template Engine

Validation

- miniwdl
- WOMtool

## 核心模型

### ToolSpec

描述一个可复用生信工具：

- name
- version
- container
- inputs
- outputs
- command

### Workflow Graph

保存可视化流程：

- nodes
- edges
- parameters

### Compiler IR

作为内部中间表示，与 WDL 版本解耦。

## 编译流程

Workflow JSON

-> Graph Validation

-> Topological Sort

-> Compiler IR

-> WDL Renderer

-> Validation

-> Export

## 技术原则

1. WDL 是输出格式，不是核心模型。
2. Tool 与 Workflow 解耦。
3. 第一阶段优先保证可复现生成。
4. 为未来 AI Agent 保留结构化接口。
