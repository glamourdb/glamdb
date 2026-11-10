if(NOT EXISTS "${PROJECT_SOURCE_DIR}/third_party/quill/CMakeLists.txt")
    message(FATAL_ERROR "Logging requires the pinned Quill submodule: git submodule update --init third_party/quill")
endif()
add_subdirectory("${PROJECT_SOURCE_DIR}/third_party/quill" "${PROJECT_BINARY_DIR}/third_party/quill"
    EXCLUDE_FROM_ALL SYSTEM)
add_library(glamdb_logging STATIC structured_log.cpp)
target_link_libraries(glamdb_logging PRIVATE "$<BUILD_INTERFACE:glamdb_build_options>" quill::quill)
target_include_directories(glamdb_logging PRIVATE "${PROJECT_SOURCE_DIR}")
