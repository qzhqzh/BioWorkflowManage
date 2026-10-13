<script setup lang="ts">
const props = defineProps<{ name: string }>()
const paths: Record<string, string> = {
    overview: '<path d="M4 20V11h3v9zm6 0V4h3v16zm6 0V8h3v12z" fill="currentColor" stroke="none"/>',
    file: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8zM14 3v6h5M8 13h8M8 17h5"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5zm-9 9 9 5 9-5M3 16l9 5 9-5"/>',
    cube: '<path d="m12 3 9 5v9l-9 5-9-5V8zm0 9 9-4M12 12 3 8m9 4v10M7.5 5.5l9 5"/>',
    code: '<path d="m7 6-5 6 5 6m10-12 5 6-5 6M14 3l-4 18"/>',
    chevron: '<path d="m9 5 7 7-7 7"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    back: '<path d="m14 5-7 7 7 7"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.3 7a8 8 0 0 1 13.2-1L20 8M4 16l1.5 2A8 8 0 0 0 18.7 17"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9a2.5 2.5 0 1 1 4 2c-1 .7-1.5 1.2-1.5 2.5M12 17h.01"/>',
    alert: '<path d="M10.3 4.6 2.5 18a1.5 1.5 0 0 0 1.3 2.2h16.4a1.5 1.5 0 0 0 1.3-2.2L13.7 4.6a2 2 0 0 0-3.4 0zM12 9v5m0 3h.01"/>',
    close: '<path d="m6 6 12 12M6 18 18 6"/>',
    flow: '<circle cx="12" cy="4" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><path d="m11 7-5 9m7-9 5 9"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    copy: '<rect x="8" y="7" width="12" height="14" rx="2"/><path d="M16 7V3H4v14h4"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6m0-10h.01"/>',
}
const shape = computed(() => paths[props.name] || paths.file)
</script>

<template>
  <svg class="ops-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" v-html="shape" />
</template>
