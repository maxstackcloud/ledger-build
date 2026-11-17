FROM eclipse-temurin:17-jre-jammy
RUN groupadd --system wezvatech && useradd --system --gid wezvatech --home-dir /opt/wezva wezvatech
WORKDIR /opt/wezva
COPY --chown=wezvatech:wezvatech target/app.jar app.jar
USER wezvatech
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "app.jar"]
