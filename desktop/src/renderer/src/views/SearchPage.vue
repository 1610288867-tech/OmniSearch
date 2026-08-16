<script setup lang="ts">
import SearchBar from "../components/SearchBar.vue";
import FilterChips from "../components/FilterChips.vue";
import ResultList from "../components/ResultList.vue";
</script>

<template>
  <div class="search-page">
    <SearchBar />
    <FilterChips />
    <ResultList />
  </div>
</template>

<style scoped>
.search-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
</style>
