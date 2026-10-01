import { createElement } from "react";
import { Text } from "react-native";
import { AuthCancelled, createAuth } from "@swapp1990/auth-expo";
import { AuthProvider, useAuth } from "@swapp1990/auth-expo/react";

const auth = createAuth({
  appId: "fixture",
  resource: "https://fixture.swapp1990.org",
  redirectUri: "authexpofixture://callback",
});

function Status() {
  const { ready, signedIn } = useAuth();
  return createElement(Text, null, `${ready} ${signedIn} ${AuthCancelled.name}`);
}

export default function App() {
  return createElement(AuthProvider, { auth }, createElement(Status));
}
