find_package(Threads REQUIRED)
add_library(glamdb_logging STATIC structured_log.cpp)
target_link_libraries(glamdb_logging PRIVATE "$<BUILD_INTERFACE:glamdb_build_options>" PUBLIC Threads::Threads)
target_include_directories(glamdb_logging PRIVATE "${PROJECT_SOURCE_DIR}")
